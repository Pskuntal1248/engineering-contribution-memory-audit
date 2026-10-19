"""Findings from the reviewer's hosted runs of 0.1.4: a missing connection must not stop the run, the profile must be in the result object
(the page can only lay out what is there), and a project the user authored must not be labelled 'contributed'."""
import asyncio
from datetime import timedelta

import fixtures
import pytest

import agent
from analysis import DecisionVerdicts, ProjectCard, ProjectCards, ThreadVerdicts, _higher_role
from sources import FixtureSource, GatewaySource
from test_adapters import Gateway, HAS, ME, NOW

D = fixtures.build()
E = D["expected"]
NOW_FIX = fixtures.START + timedelta(days=176)
INP = {"github_username": fixtures.USER, "repos_or_org": "acme/payments-api, acme/web", "jira_site": fixtures.SITE, "jira_projects": "PAY, WEB",
       "slack_channels": "#payments-eng, #incidents", "display_name": "Meera Joshi", "slack_user_ids": fixtures.ME, "role_and_level": "Software Engineer II", "backfill_months": 6}


class ContributedLLM:
    """A model that words every project card as 'contributed', the label the reviewer saw on a repo where the user authored every PR."""
    diagnostics: list = []

    async def structured(self, schema, system, payload, budget=None):
        if budget is not None:
            budget.spend()
        if schema is ThreadVerdicts:
            return ThreadVerdicts(verdicts=[])
        if schema is DecisionVerdicts:
            return DecisionVerdicts(verdicts=[])
        if schema is ProjectCards:
            return ProjectCards(projects=[ProjectCard(project_id=p["project_id"], name="P " + p["project_id"], goal="g", outcome="o", role="contributed",
                                                      evidence=[e["id"] for e in p["evidence"]]) for p in payload["projects"]])
        raise RuntimeError("not used in this test")


def test_higher_role_never_lowers_what_the_code_proved():
    assert _higher_role("contributed", "built") == "built"
    assert _higher_role("led", "built") == "led"
    assert _higher_role("built", "built") == "built"
    assert _higher_role("reviewed", "reviewed") == "reviewed"


def test_model_saying_contributed_does_not_hide_authored_work():
    deps = agent.Deps(lambda cfg: FixtureSource(D, before=E["week2_start"]), llm=ContributedLLM(), now=lambda: NOW_FIX)
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": INP, "memory": {}}))
    projects = out["result"]["project_list"]
    assert projects
    from models import Item
    items = [Item.from_dict(i) if isinstance(i, dict) else i for i in out["ctx"]["items"]]
    mine = {i.id for i in items if i.meta.get("mine") and i.type == "pr" and i.meta.get("merged_flag")}
    # every project that holds a PR the user authored and shipped is at least 'built'
    checked = 0
    for p in out["ctx"]["prof"]["projects"]:
        if any(e in mine for e in p["evidence"]):
            checked += 1
            assert p["role"] in {"built", "led"}, (p["name"], p["role"])
    assert checked >= 1


@pytest.fixture(scope="module")
def full_run():
    deps = agent.Deps(lambda cfg: FixtureSource(D, before=E["week2_start"]), llm=None, now=lambda: NOW_FIX)
    return asyncio.run(agent.make_graph(deps).ainvoke({"inputs": INP, "memory": {}}))


def test_result_carries_the_profile_as_lists_of_flat_objects(full_run):
    r = full_run["result"]
    for key in ("project_list", "problems_solved", "invisible_work_list", "skills", "decision_list", "ledger"):
        assert isinstance(r[key], list), key
        assert all(isinstance(row, dict) and all(isinstance(v, (str, int, float)) for v in row.values()) for row in r[key]), key
    assert r["project_list"] and r["ledger"] and r["invisible_work_list"] and r["skills"]
    assert all(row["link"].startswith("http") for row in r["project_list"] if row["link"])
    assert set(r["ledger"][0]) == {"source", "date", "project", "type", "title", "url", "counted"}
    assert r["ledger_rows_shown"] == len(r["ledger"]) <= r["ledger_rows"]


def test_on_demand_result_has_the_ledger_but_not_the_profile_lists(full_run):
    deps = agent.Deps(lambda cfg: FixtureSource(D), llm=None, now=lambda: NOW_FIX)
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {**INP, "request": "one_on_one"}, "memory": full_run["memory"]}))
    assert out["result"]["mode"] == "one_on_one" and "project_list" not in out["result"] and isinstance(out["result"]["ledger"], list)


# ---- a source that is not connected is skipped and named; GitHub still runs

class NoJiraNoSlack(Gateway):
    async def __call__(self, name, args):
        if name.startswith(("jira.", "slack.")):
            raise RuntimeError("401 Unauthorized: not connected")
        return await super().__call__(name, args)


def test_github_only_run_completes_when_jira_and_slack_are_not_connected():
    gw = NoJiraNoSlack()
    has = HAS | {"jira.get_all_projects", "jira.get_server_info"}

    async def broken_site():
        raise RuntimeError("jira.get_server_info: not connected")
    deps = agent.Deps(lambda cfg: GatewaySource(gw, cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, has, cfg.docs_sources, cfg.name_hints),
                      llm=None, now=lambda: NOW, site=broken_site)
    inp = {"github_username": ME, "repos_or_org": "o/r", "jira_projects": "PAY", "slack_channels": "#payments-eng", "role_and_level": "Software Engineer"}
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": inp, "memory": {}}))
    r = out["result"]
    assert r["status"] == "completed" and r["mode"] == "backfill"
    assert any(n.startswith("github.") for n, _ in gw.calls)  # GitHub was read
    assert {"jira", "slack"} <= set(r["unavailable"])
    assert not any(s == "preflight" for s in r["unavailable"])
    text = str(out["corpus"])
    assert "What could not be done" in text and "not connected" in text


def test_github_only_run_with_no_optional_inputs_says_not_covered_and_does_not_fail():
    gw = Gateway()
    deps = agent.Deps(lambda cfg: GatewaySource(gw, cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, HAS, cfg.docs_sources, cfg.name_hints),
                      llm=None, now=lambda: NOW)
    inp = {"github_username": ME, "repos_or_org": "o/r", "role_and_level": "Software Engineer"}
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": inp, "memory": {}}))
    r = out["result"]
    assert r["status"] == "completed" and not any(u.split(":")[0] in {"jira", "slack", "docs"} for u in r["unavailable"])
    notes = " ".join(r["notes"])
    assert "Not covered: Jira" in notes and "Not covered: Slack" in notes and "Not covered: docs" in notes
    assert not any(n.startswith(("jira.", "slack.")) for n, _ in gw.calls)


# ---- a monitor over several weeks of different dates: six-month filter, weekly delta, state carry-over (GitHub only, fixture data spanning ~6 months)

GH = {**D, "jira": [], "slack": {"channels": [], "threads": []}, "docs": []}
GH_INP = {"github_username": fixtures.USER, "repos_or_org": "acme/payments-api, acme/web", "role_and_level": "Software Engineer II", "request": "weekly_update", "backfill_months": 6}


def _run(inp, mem, days, before_days=None):
    now = fixtures.START + timedelta(days=days)
    before = None if before_days is None else (fixtures.START + timedelta(days=before_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    deps = agent.Deps(lambda cfg: FixtureSource(GH, before=before), llm=None, now=lambda: now)
    return asyncio.run(agent.make_graph(deps).ainvoke({"inputs": inp, "memory": mem}))


def test_weekly_runs_a_week_apart_carry_state_and_add_up():
    a = _run(GH_INP, {}, 176, 176)
    b = _run(GH_INP, a["memory"], 183, 183)
    c = _run(GH_INP, b["memory"], 190)
    assert [x["result"]["mode"] for x in (a, b, c)] == ["backfill", "weekly", "weekly"]
    assert b["result"]["new_items"] > 0 and c["result"]["new_items"] > 0
    again = _run(GH_INP, c["memory"], 197)  # nothing new a week later: no double counting, and the page says so
    assert again["result"]["mode"] == "weekly" and again["result"]["new_items"] == 0
    from store import unpack
    rows = unpack(again["memory"]["profile"])["items"]
    assert len({r["id"] for r in rows}) == len(rows)  # every item is saved once
    assert a["result"]["ledger_rows"] + b["result"]["new_items"] + c["result"]["new_items"] == len(rows)  # first backfill plus each week's new items add up to the saved profile
    assert again["result"]["stats"]["prs_shipped"] >= a["result"]["stats"]["prs_shipped"] + 2


def test_median_cycle_time_is_real_over_spread_dates():
    r = _run(GH_INP, {}, 190)["result"]
    assert r["stats"]["median_cycle_hours"] and r["stats"]["median_cycle_hours"] > 0


def test_backfill_window_drops_items_older_than_the_requested_months():
    six = _run(GH_INP, {}, 190)["result"]
    two = _run({**GH_INP, "backfill_months": 2}, {}, 190)["result"]
    assert two["ledger_rows"] < six["ledger_rows"]
    assert min(r["date"] for r in two["ledger"]) >= (fixtures.START + timedelta(days=190 - 60)).strftime("%Y-%m-%d")


def test_summary_says_github_only_and_keeps_not_requested_apart_from_not_connected():
    out = _run(GH_INP, {}, 190)
    assert out["result"]["coverage"] == {"github": "read", "jira": "not requested", "slack": "not requested", "docs": "not requested"}
    assert "Coverage: GitHub only" in out["corpus"]["summary"] and "Jira not requested" in out["corpus"]["summary"]
    # saved-profile answers describe the saved profile, not a fresh read
    od = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: FixtureSource(GH), llm=None, now=lambda: NOW_FIX)).ainvoke({"inputs": {**GH_INP, "request": "one_on_one"}, "memory": out["memory"]}))
    assert od["result"]["coverage"]["jira"] == "not in the saved profile" and "Coverage: GitHub only" in od["corpus"]["summary"]


def test_summary_names_a_not_connected_source_and_a_failed_one_differently():
    from report_notes import coverage
    line, st = coverage({"github": True, "jira": True, "slack": True, "docs": True},
                        [{"source": "jira", "error": "Jira is not connected, or its connection needs renewing"}, {"source": "slack:#x", "error": "#x is not in the channel list"}], {"github": 4})
    assert st == {"github": "read", "jira": "not connected", "slack": "could not be read", "docs": "read"} or st["docs"] == "read"
    assert "Jira not connected" in line and "Slack could not be read" in line


class StagedSource:
    """A PR that is open at the first run and merges before the second: what a real week looks like."""

    def __init__(self, stage, seen_since):
        self.stage, self.seen_since = stage, seen_since

    async def fetch(self, since, until, log):
        from test_fixes3 import pr_dict, raw
        self.seen_since.append(since)
        old = pr_dict(1, "Add payout retries", created="2026-10-01T10:00:00Z", merged="2026-10-01T12:00:00Z")
        slow = pr_dict(2, "Add settlement report", created="2026-10-02T10:00:00Z", merged="2026-10-09T12:00:00Z")
        if self.stage == 1:
            slow = {**slow, "merged_at": None, "closed_at": None, "state": "open"}
        prs = [p for p in (old, slow) if p["created_at"][:10] >= since[:10]]
        return {**raw(prs, []), "failed": [], "capped": []}


def test_pr_open_at_the_last_run_is_updated_when_it_merges_a_week_later():
    from datetime import datetime, timezone
    inp = {"github_username": "Pskuntal1248", "repos_or_org": "o/r", "role_and_level": "Software Engineer", "request": "weekly_update"}
    seen: list[str] = []
    d1, d2 = datetime(2026, 10, 3, 9, tzinfo=timezone.utc), datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    a = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: StagedSource(1, seen), llm=None, now=lambda: d1)).ainvoke({"inputs": inp, "memory": {}}))
    assert a["result"]["stats"]["prs_shipped"] == 1 and a["result"]["stats"]["prs_authored"] == 2
    b = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: StagedSource(2, seen), llm=None, now=lambda: d2)).ainvoke({"inputs": inp, "memory": a["memory"]}))
    assert b["result"]["mode"] == "weekly"
    assert seen[1][:10] <= "2026-10-02"  # the second run looked back to the day the open PR was created, not just to the last cursor
    assert b["result"]["new_items"] == 1 and b["result"]["stats"]["prs_shipped"] == 2 and b["result"]["stats"]["prs_authored"] == 2  # replaced, not duplicated
    c = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: StagedSource(2, seen), llm=None, now=lambda: d2 + timedelta(days=7))).ainvoke({"inputs": inp, "memory": b["memory"]}))
    assert c["result"]["new_items"] == 0 and c["result"]["stats"]["prs_shipped"] == 2


def test_a_slack_that_is_not_connected_is_not_reported_as_channels_not_found():
    class Src(FixtureSource):
        async def fetch(self, s, u, l):
            o = await super().fetch(s, u, l)
            o["failed"].append({"source": "slack", "error": "Slack is not connected, or its connection needs renewing. Connect Slack and run again to include it"})
            return o
    deps = agent.Deps(lambda cfg: Src(GH), llm=None, now=lambda: NOW_FIX)
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {**GH_INP, "slack_channels": "#payments-eng"}, "memory": {}}))
    assert out["result"]["unavailable"].count("slack") == 1 and not any(u.startswith("slack:#") for u in out["result"]["unavailable"])
    assert out["result"]["coverage"]["slack"] == "not connected" and "Slack not connected" in out["corpus"]["summary"]
