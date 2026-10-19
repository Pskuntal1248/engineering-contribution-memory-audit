"""The user's real agents-testing repo (GitHub only), pushed through the COMPILED graph with a checkpointer. Source data: the public GitHub API commits of
Pskuntal1248/agents-testing, saved as tests/real_agents_testing_commits.json. No model is called (llm=None), so this is the rule baseline, not the hosted model output."""
import asyncio
import json
import os
from datetime import datetime, timezone

import agent
from test_checkpoint_state import Hosted
from sources import FixtureSource

HERE = os.path.dirname(__file__)
REAL = json.load(open(os.path.join(HERE, "real_agents_testing_commits.json")))
REPO = "Pskuntal1248/agents-testing"
INP = {"github_username": "Pskuntal1248", "repos_or_org": REPO, "role_and_level": "Software Engineer", "request": "weekly_update", "backfill_months": 6}
NOW = datetime(2026, 10, 19, 22, 0, tzinfo=timezone.utc)  # about when the user ran it (03:40 IST paste on Oct 20); the run's exact start is not known


class RealSource(FixtureSource):
    def __init__(self, fail_jira=False):
        super().__init__({"github": {}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": []})
        self.fail_jira = fail_jira

    async def fetch(self, since, until, log):
        commits = [dict(c, _repo=REPO) for c in REAL["commits"] if c["commit"]["author"]["date"] >= since]
        out = {"github": {"prs": [REAL["pr"]], "reviews": {}, "commits": commits}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": [], "failed": [], "capped": [], "info": []}
        if self.fail_jira:
            out["failed"].append({"source": "jira", "error": "Jira is not connected (tool said: 401)"})
        return out


def run(fail_jira=False, inp=INP, memory=None, now=NOW, h=None):
    with (h or Hosted()):
        g = agent.make_graph(agent.Deps(lambda cfg: RealSource(fail_jira), llm=None, now=lambda: now))
        return asyncio.run(g.ainvoke({"inputs": dict(inp), "memory": memory or {}}, {"configurable": {"thread_id": "real1"}}))


def sections(out):
    return {s["heading"]: s for s in out["corpus"]["sections"]}


def test_real_commits_through_the_checkpointed_graph():
    out = run()
    secs = sections(out)
    assert out["result"]["mode"] == "backfill"
    assert "132 items: 131 commits, 1 PR" in secs["Appendix: item ledger (also the CSV download)"]["body"]  # 142 on the API, 11 before the 180-day window
    skills = secs["Skills"]
    names = str(skills)
    assert "technical design" not in names and "accessibility" not in names and "frontend" in names
    solved = str(secs["Problems solved"])
    for t in ("Fix agent resolution pipeline", "Stop double-counting rebuildAndRedeploy", "Verify tool-call capability"):
        assert t in solved
    assert "Slack (not requested)" in str(secs["Invisible work"])
    notes = str(out["corpus"])
    assert notes.count("Not covered: Slack") == 1
    assert "What could not be done" not in secs or "failed" not in str(secs["What could not be done"])
    assert "window: work dated since 2026-04-2" in notes


def test_real_commits_failure_path_names_the_failed_source_and_still_returns():
    out = run(fail_jira=True, inp={**INP, "jira_projects": "PAY"})
    secs = sections(out)
    assert "What could not be done" in secs and "Jira" in str(secs["What could not be done"])
    assert "Problems solved" in secs


def test_real_commits_weekly_resume_from_checkpointed_memory():
    h = Hosted()  # one shared store, like the platform
    first = run(h=h)
    second = run(inp={**INP, "request": "auto"}, memory=first["memory"], now=datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc), h=h)
    assert second["result"]["mode"] == "weekly" and second["result"]["new_items"] == 0


class TopicEmbedder:
    """SYNTHETIC vectors (no model call): puts the user's commits of 2026-07-26..28 in one topic, so a project holding the Claude-trailer commit b6bc12f forms."""
    calls = 1
    last = {"attempts": 1, "failed_attempts": 0}

    async def embed(self, items, budget=None, reserve=0):
        return {i.id: ([1.0, 0.0] if "2026-07-26" <= i.date[:10] <= "2026-07-28" else [0.0, 1.0]) for i in items}, []


def test_claude_trailer_commit_in_a_project_gives_separate_ai_credit():
    from eprofile import build
    from guards import budget_for
    from normalize import normalize_commit
    from outputs import corpus_for
    its = [x for x in (normalize_commit(dict(c, _repo=REPO), REPO, "Pskuntal1248", []) for c in REAL["commits"]) if hasattr(x, "meta")]
    prof = asyncio.run(build(its, username="Pskuntal1248", names={"Pskuntal1248"}, llm=None, budget=budget_for("backfill"), framework="", role_and_level="SE", prior=None, embedder=TopicEmbedder()))
    hit = [p for p in prof["projects"] if any("b6bc12f" in e for e in p["evidence"])]
    assert hit and hit[0]["ai_assist"] == ["Claude Opus 4.8"] and not any("Claude" in c for c in hit[0]["co_contributors"])
    corpus, _ = corpus_for("backfill", prof, its, title="t", terms=set(), keep_internal=False, colleagues=[], names={"Pskuntal1248"}, date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set())
    text = str(next(s for s in corpus["sections"] if s["heading"] == "Projects"))
    assert "AI assistant credited in commit trailers: Claude Opus 4.8 (not a human collaborator)" in text and "with Claude" not in text
