"""PR-commit SHA membership as hard dedup evidence, and pre-read validation of Slack channel and Jira project inputs."""
import asyncio
from datetime import datetime, timezone

from guards import ToolLog, channels_to_read, parse_channels
from pipeline import Config, ingest
from sources import GatewaySource
from test_adapters import Gateway, HAS, ME, NOW, SINCE
from test_fixes3 import CFG, commit_dict, pr_dict, raw


# ---------------------------------------------------------- SHA membership dedup

def test_pr_commit_sha_membership_folds_shadow_commit():
    # different title, different files, no shared key: the SHA being one of the PR's own commits is the definitive relation
    p = pr_dict(1, "Add result ranking", files=("src/search.py",))
    p["_commit_shas"] = ["d" * 40]
    out = ingest(raw([p], [commit_dict("d" * 40, "SRCH-202 Add result ranking v2", files=("c_search.go",))]), CFG)
    assert [i.type for i in out.items] == ["pr"]
    assert any("counted through its PR" in s.reason for s in out.skipped)


def test_commit_sha_not_in_pr_stays_separate():
    p = pr_dict(1, "Add result ranking", files=("src/search.py",))
    p["_commit_shas"] = ["e" * 40]
    out = ingest(raw([p], [commit_dict("f" * 40, "SRCH-202 Add result ranking v2", files=("c_search.go",))]), CFG)
    assert sorted(i.type for i in out.items) == ["commit", "pr"]


# ---------------------------------------------------------- Slack input validation

def test_channel_matched_by_id_as_well_as_name():
    chans = [{"id": "C0123", "name": "general"}, {"id": "C9999", "name": "payments-eng"}]
    picked, missing = channels_to_read(chans, ["C0123"], False)
    assert [c["name"] for c in picked] == ["general"] and missing == []
    picked, missing = channels_to_read(chans, ["payments-eng", "genral"], False)
    assert [c["name"] for c in picked] == ["payments-eng"] and missing == ["genral"]  # a typo is reported, never substituted


def test_unknown_channel_lists_visible_channels_and_suggests():
    gw = Gateway()
    out = asyncio.run(GatewaySource(gw, ME, [], parse_channels("payments-engr"), False, [], HAS).fetch(SINCE, None, ToolLog()))
    err = next(f["error"] for f in out["failed"] if f["source"] == "slack:#payments-engr")
    assert "Did you mean: #payments-eng?" in err
    assert "#random" in err  # the visible list is shown so the user can choose
    assert out["slack"]["threads"] == []
    assert [n for n, _ in gw.calls] == ["slack.list_channels"]  # one bad input: nothing else was called at all


def test_mixed_valid_and_unknown_channels_reads_nothing():
    # one bad channel name stops the whole read - the good channel is not read either
    gw = Gateway()
    out = asyncio.run(GatewaySource(gw, ME, [], parse_channels("payments-eng,payments-engr"), False, [], HAS).fetch(SINCE, None, ToolLog()))
    assert out["slack"]["threads"] == []
    assert [n for n, _ in gw.calls] == ["slack.list_channels"]
    assert any(f["source"] == "preflight" and "no contribution data was read at all" in f["error"] for f in out["failed"])


def test_channel_id_accepted_end_to_end():
    gw = Gateway()
    out = asyncio.run(GatewaySource(gw, ME, [], parse_channels("C1"), False, [], HAS).fetch(SINCE, None, ToolLog()))
    assert [t["channel_id"] for t in out["slack"]["threads"]] == ["C1"]


# ---------------------------------------------------------- Jira project preflight

class JiraProjects(Gateway):
    async def __call__(self, name, args):
        if name == "jira.get_all_projects":
            return {"values": [{"key": "PAY"}, {"key": "SRCH"}]}
        return await super().__call__(name, args)


def run_jira(gw, projects, has):
    return asyncio.run(GatewaySource(gw, ME, [], parse_channels("#payments-eng"), False, projects, has, [], ["Parth Singh"]).fetch(SINCE, None, ToolLog()))


def test_jira_preflight_canonicalises_case():
    gw = JiraProjects()
    out = run_jira(gw, ["pay"], HAS | {"jira.get_all_projects"})
    q = next(a for n, a in gw.calls if n == "jira.search_issues")
    assert "project in (PAY)" in q["jql"]  # the lowercase input resolves to the real key
    assert [t["key"] for t in out["jira"]] == ["PAY-1", "PAY-2", "PAY-3"]


def test_jira_mixed_valid_and_invalid_keys_reads_nothing():
    # one wrong key stops the whole Jira read - the valid key is not read either
    gw = JiraProjects()
    out = run_jira(gw, ["pay", "PAY2"], HAS | {"jira.get_all_projects"})
    assert out["jira"] == []
    assert not any(n == "jira.search_issues" for n, _ in gw.calls)
    err = next(f["error"] for f in out["failed"] if f["source"] == "jira:PAY2")
    assert "not visible" in err and "Did you mean: PAY?" in err and "SRCH" in err
    assert any(f["source"] == "preflight" and "no contribution data was read at all" in f["error"] for f in out["failed"])
    assert not any(n == "slack.search_messages" for n, _ in gw.calls)  # the valid Slack input is not read either


class JiraProjectsRefused(Gateway):
    async def __call__(self, name, args):
        if name == "jira.get_all_projects":
            raise RuntimeError("401 Unauthorized")
        return await super().__call__(name, args)


def test_jira_preflight_refused_does_not_block_other_sources():
    gw = JiraProjectsRefused()
    out = run_jira(gw, ["PAY"], HAS | {"jira.get_all_projects"})
    assert out["jira"] == [] and not out.get("blocked")
    assert not any(n == "jira.search_issues" for n, _ in gw.calls)
    assert out["coverage"]["jira"] == "isn't connected"


def test_jira_preflight_tool_missing_is_optional():
    gw = Gateway()
    out = run_jira(gw, ["PAY"], HAS - {"jira.get_all_projects"})
    assert out["jira"] == [] and not out.get("blocked")
    assert out["coverage"]["jira"] == "isn't available in this run"


# ------------------------------------------ the barrier is global: one bad input, zero contribution reads anywhere

DOCS = ["confluence", "notion", "google drive"]
DISCOVERY = {"slack.list_channels", "jira.get_all_projects"}  # the only calls a blocked run may make


def test_invalid_slack_blocks_github_jira_and_docs():
    gw = JiraProjects()
    out = asyncio.run(GatewaySource(gw, ME, ["o/r"], parse_channels("payments-engr"), False, ["PAY"], HAS | {"jira.get_all_projects"}, DOCS, ["Parth Singh"]).fetch(SINCE, None, ToolLog()))
    assert {n for n, _ in gw.calls} <= DISCOVERY
    assert out["github"] == {"prs": [], "reviews": {}, "commits": []} and out["jira"] == [] and out["docs"] == [] and out["slack"]["threads"] == []
    assert any(f["source"] == "preflight" for f in out["failed"])


def test_refused_jira_leaves_github_slack_and_docs_available():
    gw = JiraProjectsRefused()
    out = asyncio.run(GatewaySource(gw, ME, ["o/r"], parse_channels("#payments-eng"), False, ["PAY"], HAS | {"jira.get_all_projects"}, DOCS, ["Parth Singh"]).fetch(SINCE, None, ToolLog()))
    assert not out.get("blocked") and out["coverage"]["jira"] == "isn't connected"
    assert any(n == "github.search_issues" for n, _ in gw.calls)


def blocked_deps(gw, store):
    import agent
    from store import PlatformStore
    HAS_ALL = HAS | {"jira.get_all_projects"}
    return agent.Deps(lambda cfg: GatewaySource(gw, cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, HAS_ALL, cfg.docs_sources, cfg.name_hints),
                      llm=None, store_factory=lambda mem: PlatformStore(store, mem), now=lambda: NOW)


INP = {"github_username": ME, "repos_or_org": "o/r", "jira_projects": "PAY", "slack_channels": "#payments-eng",
       "docs_source": "All three", "role_and_level": "Software Engineer", "display_name": "Parth Singh"}


def test_blocked_first_run_saves_nothing_and_reports_blocked():
    import agent
    from langgraph.store.memory import InMemoryStore
    store = InMemoryStore()
    gw = JiraProjects()
    g = agent.make_graph(blocked_deps(gw, store))
    bad = dict(INP, slack_channels="payments-engr")
    out = asyncio.run(g.ainvoke({"inputs": bad, "memory": {}}))
    assert out["result"]["status"] == "blocked" and out["result"]["model_calls"] == 0
    assert "Run stopped" in out["corpus"]["title"]
    assert out["memory"] == {}  # nothing was persisted
    assert not asyncio.run(store.asearch(("uc10", "profile"), limit=100))
    # a later run with the input fixed backfills normally - the blocked run never marked anything done
    gw2 = JiraProjects()
    g2 = agent.make_graph(blocked_deps(gw2, store))
    out2 = asyncio.run(g2.ainvoke({"inputs": INP, "memory": {}}))
    assert out2["result"]["status"] == "completed" and out2["result"]["mode"] == "backfill" and out2["memory"]["marker"]["backfill_done"] is True


def test_blocked_weekly_run_leaves_the_saved_profile_untouched():
    import agent
    from langgraph.store.memory import InMemoryStore
    store = InMemoryStore()
    g = agent.make_graph(blocked_deps(JiraProjects(), store))
    out = asyncio.run(g.ainvoke({"inputs": INP, "memory": {}}))
    assert out["result"]["status"] == "completed"
    mem = out["memory"]
    keys_before = {i.key: i.value for i in asyncio.run(store.asearch(("uc10", "profile"), limit=100))}
    bad = dict(INP, slack_channels="payments-engr")
    out2 = asyncio.run(agent.make_graph(blocked_deps(JiraProjects(), store)).ainvoke({"inputs": bad, "memory": mem}))
    assert out2["result"]["status"] == "blocked" and out2["memory"] == mem  # saved memory passes through untouched
    keys_after = {i.key: i.value for i in asyncio.run(store.asearch(("uc10", "profile"), limit=100))}
    assert keys_after == keys_before  # the saved profile is byte-identical
    # and the next good run is still a normal weekly update on the intact profile
    out3 = asyncio.run(agent.make_graph(blocked_deps(JiraProjects(), store)).ainvoke({"inputs": INP, "memory": mem}))
    assert out3["result"]["status"] == "completed" and out3["result"]["mode"] == "weekly"
