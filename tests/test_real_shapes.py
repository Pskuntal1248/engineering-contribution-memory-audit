"""Shapes seen in the first real run: PRs with no merged_at from search, merge state only in repo history,
ADR PRs, self-comments, and fetch concurrency. Fake gateway and fake model; no real tools."""
import asyncio
from datetime import datetime, timezone

import agent
from analysis import DecisionVerdict, DecisionVerdicts
from sources import GatewaySource

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
ME = "Pskuntal1248"
INP = {"github_username": ME, "repos_or_org": "o/r", "role_and_level": "Software Engineer", "backfill_months": 6}


def pr(n, title, created, closed, paths, merged_at=None, body=""):
    return {"number": n, "title": title, "body": body, "state": "closed", "html_url": f"https://github.com/o/r/pull/{n}", "user": {"login": ME},
            "created_at": created, "closed_at": closed, "labels": [], "pull_request": {"merged_at": merged_at}, "_paths": paths}


def commit(sha, msg, date):
    return {"sha": sha, "html_url": f"https://github.com/o/r/commit/{sha}", "author": {"login": ME},
            "commit": {"message": msg, "author": {"name": ME, "date": date}}}


PRS = [
    pr(1, "Add retry to client", "2026-09-01T10:00:00Z", "2026-09-01T14:00:00Z", ["src/client.py"]),
    pr(2, "Add cache", "2026-09-02T10:00:00Z", "2026-09-03T10:00:00Z", ["src/cache.py"]),
    pr(3, "docs: ADR-001 move webhooks to a queue", "2026-09-04T10:00:00Z", "2026-09-04T12:00:00Z", ["docs/adr/001-webhook-queue.md"],
       body="We decided to move webhooks to a queue."),
    pr(4, "Closed without merging", "2026-09-05T10:00:00Z", "2026-09-05T11:00:00Z", ["src/x.py"]),
    pr(5, "With merged_at", "2026-09-06T10:00:00Z", "2026-09-06T12:00:00Z", ["src/y.py"], merged_at="2026-09-06T12:00:00Z"),
]
COMMITS = [
    commit("m1" + "0" * 38, "Merge pull request #1 from o/b1", "2026-09-01T14:00:00Z"),
    commit("m2" + "0" * 38, "Add cache (#2)", "2026-09-03T10:00:00Z"),
    commit("m3" + "0" * 38, "Merge pull request #3 from o/b3", "2026-09-04T12:00:00Z"),
    commit("b1" + "0" * 38, "wip retry", "2026-09-01T11:00:00Z"),       # branch commit of PR 1: counted through the PR
    commit("z9" + "0" * 38, "Standalone fix", "2026-09-20T11:00:00Z"),   # real direct commit
]
COMMIT_FILES = {"b1" + "0" * 38: [{"filename": "src/client.py", "additions": 5, "deletions": 0}]}


class FakeLLM:
    def __init__(self):
        self.seen = []

    async def structured(self, schema, system, payload, budget=None):
        if budget:
            budget.spend()
        self.seen.append(schema.__name__)
        if schema is DecisionVerdicts:
            return DecisionVerdicts(verdicts=[DecisionVerdict(item_id=i["item_id"], is_decision=True, summary="Move webhooks to a queue.") for i in payload["items"]])
        raise RuntimeError("unexpected schema " + schema.__name__)


def make_call(state):
    async def call(name, args):
        state["n"] += 1
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        await asyncio.sleep(0.01)
        state["live"] -= 1
        if name == "github.search_issues":
            return {"items": [dict(p) for p in PRS]} if "-author" not in args["q"] else {"items": []}
        if name == "github.list_pull_requests":
            return [{"number": p["number"], "state": "closed", "merged": False} for p in PRS]  # merged flag is false on everything, as seen live
        if name == "github.list_pr_files":
            p = next(p for p in PRS if p["number"] == args["pull_number"])
            return [{"filename": f, "additions": 10, "deletions": 1} for f in p["_paths"]]
        if name == "github.list_pr_reviews":
            if args["pull_number"] == 1:  # only my own comment on my own PR
                return [{"id": 9, "user": {"login": ME}, "state": "COMMENTED", "submitted_at": "2026-09-01T12:00:00Z", "body": "note"}]
            return []
        if name == "github.list_commits":
            return COMMITS
        if name == "github.get_commit":
            return {"files": COMMIT_FILES.get(args["ref"], [{"filename": "src/fix.py", "additions": 3, "deletions": 1}])}
        raise RuntimeError("unexpected " + name)
    return call


def run(llm=None):
    state = {"n": 0, "live": 0, "peak": 0}
    has = {"github.search_issues", "github.list_pull_requests", "github.list_pr_files", "github.list_pr_reviews", "github.list_commits", "github.get_commit"}
    deps = agent.Deps(lambda cfg: GatewaySource(make_call(state), cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, has), llm=llm, now=lambda: NOW)
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": INP, "memory": {}}))
    return out["result"], state


def test_merged_is_confirmed_from_repo_history_and_cycle_time_exists():
    out, _ = run()
    s = out["stats"]
    assert s["prs_authored"] == 5
    assert s["prs_shipped"] == 3                      # code PRs only: the merged docs-only ADR PR is a proposal, not shipped code
    assert s["median_cycle_hours"] is not None


def test_adr_pr_is_a_decision_and_model_pass_runs():
    llm = FakeLLM()
    out, _ = run(llm)
    assert out["decisions"] == 1
    assert "DecisionVerdicts" in llm.seen
    assert out["model_calls"] >= 1


def test_adr_pr_is_a_decision_without_model():
    out, _ = run(None)
    assert out["decisions"] == 1


def test_commits_inside_a_pr_and_self_comments_are_not_counted_and_reasons_are_named():
    out, _ = run()
    assert out["stats"]["commits_authored"] == 1      # only the standalone fix
    notes = " ".join(out["notes"])
    assert "Rows not counted" in notes and "own comment on own PR" in notes and "already counted through its PR" in notes
    assert out["stats"]["reviews_given"] == 0


def test_fetch_runs_calls_in_parallel_but_bounded():
    _, state = run()
    assert 1 < state["peak"] <= 10
