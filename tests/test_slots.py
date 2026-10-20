"""Optional sources as manifest slots: Off grants no tools and nothing is called for an Off source. Local only."""
import sys
sys.path.insert(0, "src")
import yaml
from gravity_schema import Manifest
from inputs import apply_slot_picks


def manifest():
    return Manifest.model_validate(yaml.safe_load(open("manifest.yaml")))


def test_default_grant_is_github_only():
    m = manifest()
    tools = m.tools_for(m.default_inputs())
    assert tools and all(t.startswith("github.") for t in tools)


def test_on_adapters_grant_only_their_own_tools():
    m = manifest()
    t = m.tools_for({"jira": "on", "notion": "on"})
    assert any(x.startswith("jira.") for x in t) and any(x.startswith("notion.") for x in t)
    assert not any(x.startswith(("slack.", "confluence.", "drive.")) for x in t)


def test_off_source_drops_typed_keys_with_a_note():
    out, notes = apply_slot_picks({"jira": "off", "jira_projects": "PAY", "slack": "off", "slack_channels": "#a", "confluence": "on", "notion": "on", "google_drive": "on"})
    assert out["jira_projects"] == "" and out["slack_channels"] == "" and out["docs_source"] == "All three"
    assert any("Jira is Off" in n for n in notes) and any("Slack is Off" in n for n in notes)


def test_missing_pick_keeps_the_older_behaviour():
    out, notes = apply_slot_picks({"jira_projects": "PAY"})
    assert out["jira_projects"] == "PAY" and not notes


import asyncio
from datetime import datetime, timezone
import agent
from sources import GatewaySource


def _run(inputs, granted):
    calls = []

    async def call(name, args):
        calls.append(name)
        if name not in granted:
            raise RuntimeError(f"{name} is not allowed in this run")
        return {"items": [], "commits": [], "login": "Pskuntal1248"}

    def factory(cfg):
        return GatewaySource(call, cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, set(granted), cfg.docs_sources, cfg.name_hints)

    async def site():
        calls.append("jira.get_server_info")
        return "https://x.atlassian.net"
    deps = agent.Deps(factory, llm=None, now=lambda: datetime(2026, 10, 20, 9, tzinfo=timezone.utc), site=site)
    base = {"github_username": "Pskuntal1248", "repos_or_org": "o/r", "role_and_level": "Software Engineer", "request": "weekly_update"}
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {**base, **inputs}, "memory": {}}))
    return calls, out


def _tools(*prefixes):
    m = manifest()
    return {t for t in m.tools_for({"jira": "on", "slack": "on", "confluence": "on", "notion": "on", "google_drive": "on"}) if t.startswith(prefixes)}


def test_graph_with_jira_slack_docs_off_calls_only_github():
    granted = set(manifest().tools_for(manifest().default_inputs()))
    calls, out = _run({"jira": "off", "jira_projects": "PAY", "slack": "off", "slack_channels": "#a", "confluence": "off", "notion": "off", "google_drive": "off"}, granted)
    assert calls and all(c.startswith("github.") for c in calls), calls
    assert not any(c.startswith(("jira.", "slack.", "confluence.", "notion.", "drive.")) for c in calls)
    assert "Jira is Off" in str(out["corpus"]) or "Jira is Off" in " ".join(out["result"].get("notes", []))


def test_graph_with_jira_on_reads_jira_and_nothing_else_extra():
    m = manifest()
    granted = set(m.tools_for({"jira": "on"}))
    calls, _ = _run({"jira": "on", "jira_projects": "PAY"}, granted)
    assert any(c.startswith("jira.") for c in calls) and not any(c.startswith(("slack.", "confluence.", "notion.", "drive.")) for c in calls)


def test_missing_pick_with_default_off_grant_makes_no_jira_call_for_reading():
    granted = set(manifest().tools_for(manifest().default_inputs()))
    calls, out = _run({"jira_projects": "PAY"}, granted)
    assert not any(c.startswith("jira.search_issues") for c in calls)


def test_docs_pairs_map_to_existing_docs_source():
    f = lambda **k: apply_slot_picks(k)[0]["docs_source"]
    assert f(notion="on") == "Notion" and f(confluence="on", google_drive="on") == "Confluence + Google Drive"
    assert f(confluence="off", notion="off", google_drive="off") == "None"


def test_each_docs_slot_grants_only_its_tools():
    m = manifest()
    t = m.tools_for({"google_drive": "on"})
    assert any(x.startswith("drive.") for x in t) and not any(x.startswith(("notion.", "confluence.")) for x in t)
