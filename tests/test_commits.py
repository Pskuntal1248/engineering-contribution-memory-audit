"""Commit ingestion, Slack-only-when-asked, and memory after an empty run. Uses a fake gateway `call`; no real tools or models."""
import asyncio
from datetime import datetime, timezone

import agent
from guards import ToolLog
from sources import GatewaySource

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
ME = "Pskuntal1248"


def commit(sha, msg, login=ME, date="2026-09-20T10:00:00Z"):
    return {"sha": sha, "html_url": f"https://github.com/o/r/commit/{sha}", "author": {"login": login},
            "commit": {"message": msg, "author": {"name": login, "date": date}}}


COMMITS = [
    commit("a" * 40, "Add retry to client"),
    commit("b" * 40, "Bump deps\n\nCo-authored-by: Sam Lee <sam@x.io>"),
    commit("c" * 40, "Update lockfile"),
    commit("d" * 40, "Revert \"Add retry to client\"\n\nThis reverts commit " + "a" * 40 + "."),
    commit("e" * 40, "chore: bump", login="dependabot[bot]"),
    commit("f" * 40, "Merge pull request #5 from o/x"),
    commit("1" * 40, "Someone else's fix", login="other-dev"),
]
FILES = {"c" * 40: [{"filename": "package-lock.json", "additions": 5000, "deletions": 4000}],
         "a" * 40: [{"filename": "src/client.py", "additions": 30, "deletions": 2}]}


def make_call(log):
    async def call(name, args):
        log.append(name)
        if name == "github.list_commits":
            return COMMITS
        if name == "github.get_commit":
            return {"files": FILES.get(args["ref"], [{"filename": "x.py", "additions": 3, "deletions": 1}])}
        if name == "github.search_issues":
            return {"items": [], "total_count": 0}
        raise RuntimeError("unexpected " + name)
    return call


def run(inputs, memory=None, calls=None):
    calls = calls if calls is not None else []
    has = {"github.list_commits", "github.get_commit", "github.search_issues"}
    deps = agent.Deps(lambda cfg: GatewaySource(make_call(calls), cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, has), llm=None, now=lambda: NOW)
    return asyncio.run(agent.make_graph(deps).ainvoke({"inputs": inputs, "memory": memory or {}}))


INP = {"github_username": ME, "repos_or_org": "o/r", "role_and_level": "Software Engineer", "backfill_months": 6}


def ledger(out):
    sec = next(s for s in out["corpus"]["sections"] if s.get("table"))
    return {r[4]: r for r in sec["table"]["rows"]}  # keyed by title


def test_repo_without_prs_still_shows_commits_and_filters():
    calls = []
    out = run(INP, calls=calls)
    rows = ledger(out)
    assert "Add retry to client" in rows and "Update lockfile" in rows
    assert rows["Add retry to client"][6] == "reverted (not counted)"
    assert rows['Revert "Add retry to client"'][6] == "revert (not counted)"
    assert out["result"]["stats"]["commits_authored"] == 2  # b and c
    assert not any(c.startswith("slack.") for c in calls)  # no slack_channels given: no Slack call


def test_lockfile_commit_size_excluded():
    out = run(INP)
    item = [i for i in out["ctx"]["items"] if i.id.endswith("c" * 10)][0]
    assert item.meta["size"]["lines"] < 100


def test_co_author_credited():
    out = run(INP)
    item = [i for i in out["ctx"]["items"] if i.id.endswith("b" * 10)][0]
    assert item.co_authors == ["Sam Lee"]


def test_empty_run_still_saves_memory_and_followup_reads_it():
    class Empty:
        pass
    calls = []

    async def call(name, args):
        calls.append(name)
        return {"items": []} if name != "github.list_commits" else []
    has = {"github.list_commits", "github.get_commit", "github.search_issues"}
    deps = agent.Deps(lambda cfg: GatewaySource(call, cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, has), llm=None, now=lambda: NOW)
    g = agent.make_graph(deps)
    first = asyncio.run(g.ainvoke({"inputs": INP, "memory": {}}))
    assert first["memory"] and first["result"]["mode"] == "backfill"
    n = len(calls)
    second = asyncio.run(g.ainvoke({"inputs": {**INP, "request": "one_on_one"}, "memory": first["memory"]}))
    assert second["result"]["mode"] == "one_on_one" and len(calls) == n  # answered from memory, no source reads


def test_slack_called_only_when_channels_given():
    calls = []

    async def call(name, args):
        calls.append(name)
        return [] if name != "slack.list_channels" else {"channels": []}
    has = {"github.list_commits", "slack.list_channels"}
    src = GatewaySource(call, ME, ["o/r"], [], False, [], has)
    asyncio.run(src.fetch("2026-04-01T00:00:00Z", None, ToolLog()))
    assert not any(c.startswith("slack.") for c in calls)
    src = GatewaySource(call, ME, ["o/r"], ["#a"], False, [], has)
    asyncio.run(src.fetch("2026-04-01T00:00:00Z", None, ToolLog()))
    assert "slack.list_channels" in calls


def _flat(out):
    return str(out["corpus"])


def test_weekly_summary_has_the_four_spec_sections_from_saved_memory():
    first = run(INP)
    out = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: None, llm=None, now=lambda: NOW)).ainvoke(
        {"inputs": {**INP, "request": "weekly_summary"}, "memory": first["memory"]}))
    heads = [s["heading"] for s in out["corpus"]["sections"]]
    assert out["result"]["mode"] == "weekly_summary"
    for h in ("Shipped", "In progress", "Problems solved", "Help given", "What changed this week", "Nothing new this week"):
        pass
    assert "Shipped" in heads or "Nothing new this week" in heads  # commits are older than 7 days here: one-line quiet week
    assert "Nothing new this week" in heads


def test_one_on_one_has_blockers_when_open_pr_exists():
    from models import Item
    from outputs import one_on_one
    it = Item(id="gh:pr:o/r#1", source="github", type="pr", date="2026-09-01T00:00:00Z", title="WIP", url="u", meta={"mine": True, "state": "open"})
    out = one_on_one({"gaps": [], "projects": [], "invisible": [], "decisions": [], "improvements": []}, {it.id: it}, [])
    assert [s["heading"] for s in out] == ["Working on"] and "- u" in out[0]["body"] and "still open" in out[0]["body"]


def test_weekly_summary_lists_shipped_commits_in_window():
    first = run(INP)
    soon = datetime(2026, 9, 24, tzinfo=timezone.utc)
    out = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: None, llm=None, now=lambda: soon)).ainvoke(
        {"inputs": {**INP, "request": "weekly_summary"}, "memory": first["memory"]}))
    sec = {s["heading"]: s for s in out["corpus"]["sections"]}
    assert "Shipped" in sec and "Bump deps" in sec["Shipped"]["body"]
    assert "Add retry" not in sec["Shipped"]["body"]  # reverted, not counted


MSGS = ["Add HTTP client retry logic", "Fix retry backoff bug in client", "Add timeout option to client", "Update README with client usage", "Add README badges and install docs",
        "Fix typo in readme", "Add session cookie handling", "Fix cookie jar expiry", "Add unit tests for client retry", "Add tests for timeout", "Initial commit", "Set up CI workflow", "Fix CI python version"]
PATHS = {"retry": "client/retry.py", "backoff": "client/retry.py", "timeout": "client/timeouts.py", "readme": "README.md", "cookie": "client/cookies.py", "CI": ".github/workflows/ci.yml", "HTTP": "client/core.py"}


def commit_only_call(calls):
    import hashlib

    rows = [commit(hashlib.sha1(m.encode()).hexdigest(), m, date=f"2026-0{1 + k // 5}-{10 + k:02d}T10:00:00Z") for k, m in enumerate(MSGS)]

    async def call(name, args):
        calls.append(name)
        if name == "github.list_commits":
            return rows
        if name == "github.get_commit":
            m = next(r["commit"]["message"] for r in rows if r["sha"] == args["ref"])
            fs = [v for k, v in PATHS.items() if k.lower() in m.lower()][:2] or ["client/core.py"]
            return {"files": [{"filename": f, "additions": 5, "deletions": 1} for f in fs]}
        return {"items": []}
    return call


def commit_only_run(inputs, memory, calls):
    has = {"github.list_commits", "github.get_commit", "github.search_issues"}
    call = commit_only_call(calls)
    deps = agent.Deps(lambda cfg: GatewaySource(call, cfg.username, cfg.repos, [], False, [], has), llm=None, now=lambda: NOW)
    return asyncio.run(agent.make_graph(deps).ainvoke({"inputs": inputs, "memory": memory}))


def test_commit_only_repo_is_grouped_into_projects_without_inflated_roles():
    out = run_commits = commit_only_run(INP, {}, [])
    projects = out["result"]["projects"] if "projects" in out["result"] else None
    assert projects, out["result"]
    grouped = sum(1 for r in ledger(out).values() if r[2] not in ("(unlinked)", "(other work)"))
    assert grouped >= 10  # of 13 commits
    roles = {p["role"] for p in out["ctx"]["prof"]["projects"]}
    assert "led" not in roles


def test_auto_is_backfill_until_a_backfill_completed():
    old_empty = {"items": [], "last_run": "2026-10-10", "cursors": {}, "seen": []}  # saved by an earlier run that never was a backfill
    calls = []
    out = commit_only_run(INP, old_empty, calls)
    assert out["result"]["mode"] == "backfill" and out["memory"]
    again = commit_only_run(INP, out["memory"], [])
    assert again["result"]["mode"] == "weekly"
