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
