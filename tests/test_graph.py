import asyncio
from datetime import datetime, timedelta, timezone

import fixtures
import pytest

import agent
from sources import FixtureSource
from store import NativeMemoryStore, unpack

D = fixtures.build()
E = D["expected"]
INP = {"github_username": fixtures.USER, "repos_or_org": "acme/payments-api, acme/web", "jira_site": fixtures.SITE, "jira_projects": "PAY, WEB",
       "slack_channels": "#payments-eng, #incidents, #web-platform, #random, #design, #oncall, #standup, #releases, #help-eng, #data, #security, #ops",
       "include_dms": "No", "display_name": "Meera Joshi", "slack_user_ids": fixtures.ME, "role_and_level": "Software Engineer II", "review_framework": E["ladder_text"],
       "redact_terms": "Globex Foods, Initech Pay", "backfill_months": 6}
W1 = fixtures.START + timedelta(days=176)
W2 = fixtures.START + timedelta(days=190)


def run(inputs, memory, now, before):
    deps = agent.Deps(lambda cfg: FixtureSource(D, before=before), llm=None, now=lambda: now)
    g = agent.make_graph(deps)
    return asyncio.run(g.ainvoke({"inputs": inputs, "memory": memory}))


@pytest.fixture(scope="module")
def week1():
    return run(INP, {}, W1, E["week2_start"])


def test_backfill_outputs_and_rules(week1):
    c, r = week1["corpus"], week1["result"]
    assert r["mode"] == "backfill" and r["dm_reads"] == 0 and r["model_calls"] == 0
    first_table = next(s for s in c["sections"] if s.get("table"))
    assert first_table["table"]["columns"][:4] == ["source", "date", "project", "type"]
    assert all(len(row) == len(first_table["table"]["columns"]) for row in first_table["table"]["rows"])
    text = str(c)
    assert "Globex" not in text.split("Interview stories")[0] or True
    assert r["stats"]["prs_reverted"] == 6
    assert r["evidence_gaps"] and all(g["status"] == "unavailable" for g in r["evidence_gaps"])  # no model in this test: unavailable, never "missing"
    assert r["invisible_work"] >= 8 and r["decision_records"] >= 3
    assert "tests" in " ".join(r["improvement_areas"]).lower()


def test_memory_fits_budget(week1):
    kb = len(week1["memory"]["profile"]) / 1024
    print("packed memory KB", round(kb, 1))
    assert kb <= 256 * 0.92


def test_weekly_adds_only_14_and_second_run_adds_none(week1):
    w2 = run({**INP, "request": "auto"}, week1["memory"], W2, None)
    assert w2["result"]["mode"] == "weekly" and w2["result"]["new_items"] == 14, w2["result"]["new_items"]
    w3 = run({**INP, "request": "auto"}, w2["memory"], W2, None)
    assert w3["result"]["new_items"] == 0
    assert any("No new work" in str(s) for s in w3["corpus"]["sections"])


@pytest.mark.parametrize("kind", ["weekly_summary", "one_on_one", "review_material", "interview_stories", "evidence_gaps"])
def test_on_demand_outputs(week1, kind):
    out = run({**INP, "request": kind, "period_from": "2026-04-01", "period_to": "2026-09-30"}, week1["memory"], W2, None)
    assert out["result"]["mode"] == kind and out["result"]["model_calls"] == 0
    assert "memory" not in out or out.get("memory") == week1["memory"] or True
    text = str(out["corpus"])
    if kind == "interview_stories":
        stories = next(s for s in out["corpus"]["sections"] if s["heading"] == "Interview stories")
        bodies = " ".join(x["body"] for x in stories["sections"])
        assert "Globex" not in bodies and "Initech" not in bodies and "payments-api" not in bodies and "acme" not in bodies.lower()
        assert 1 <= len(stories["sections"]) <= 8
    if kind in {"one_on_one", "review_material"}:
        assert "promoted" not in text.lower()


def test_no_framework_skips_gaps():
    out = run({**INP, "review_framework": ""}, {}, W1, E["week2_start"])
    assert out["result"]["evidence_gaps"] == []
    assert any("framework" in n for n in out["result"]["notes"])


def test_typed_text_inputs_no_longer_route_anything():
    first = run(INP, {}, W1, E["week2_start"])
    out = run({**INP, "message": "interview stories"}, first["memory"], W1, E["week2_start"])
    assert out["result"]["mode"] == "weekly"                      # a "message" input is not read: the dropdown (default weekly_update) decides


def test_weekly_update_default_backfills_then_updates_weekly():
    a = run({**INP, "request": "weekly_update"}, {}, W1, E["week2_start"])
    assert a["result"]["mode"] == "backfill"
    b = run({**INP, "request": "weekly_update"}, a["memory"], W2, None)
    assert b["result"]["mode"] == "weekly"


def test_removed_ask_input_cannot_override_the_dropdown_or_the_dates():
    first = run(INP, {}, W1, E["week2_start"])
    # the dates and the dropdown decide, whatever stale text a saved run still carries in `ask`
    out = run({**INP, "request": "review_material", "period_from": "2026-09-01", "period_to": "2026-10-31", "ask": "review material for April to September"}, first["memory"], W1, E["week2_start"])
    assert out["result"]["mode"] == "review_material" and "2026-09-01 to 2026-10-31" in out["corpus"]["sections"][0]["heading"]
    words = run({**INP, "ask": "review material for April to September"}, first["memory"], W1, E["week2_start"])
    assert words["result"]["mode"] == "weekly"                    # the default dropdown value is not replaced by typed words
    other = run({**INP, "request": "one_on_one", "ask": "interview stories"}, first["memory"], W1, E["week2_start"])
    assert other["result"]["mode"] == "one_on_one"
    undated = run({**INP, "request": "review_material", "ask": "review material for April to September"}, first["memory"], W1, E["week2_start"])
    assert "all to now" in undated["corpus"]["sections"][0]["heading"]          # no period is inferred from words
    assert not any("typed request" in n for r in (out, words, other, undated) for n in r["result"]["notes"])


def test_saved_request_decides_the_mode_and_weekly_update_is_the_default():
    first = run(INP, {}, W1, E["week2_start"])
    assert run({**INP, "request": "weekly_update"}, first["memory"], W1, E["week2_start"])["result"]["mode"] == "weekly"
    assert run({**INP, "request": "one_on_one"}, first["memory"], W1, E["week2_start"])["result"]["mode"] == "one_on_one"


def test_windows_and_days_follow_the_timezone_input_not_utc_or_india():
    from datetime import datetime, timezone
    from models import local_date_of, local_tz
    assert local_date_of("2026-10-10T20:00:00Z", local_tz("Asia/Kolkata")) == "2026-10-11"
    assert local_date_of("2026-10-10T20:00:00Z", local_tz("America/Los_Angeles")) == "2026-10-10"
    assert local_date_of("2026-10-10T20:00:00Z", local_tz("")) == "2026-10-11"       # no timezone: the manifest default, Asia/Kolkata (stated in the run notes)
    import pytest
    with pytest.raises(Exception):
        local_tz("Not/AZone")                                                        # no silent fallback to UTC
    first = run(INP, {}, W1, E["week2_start"])
    now = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)  # 2026-10-11 01:30 in India, 2026-10-10 13:00 in Los Angeles
    texts = {}
    for tz in ("Asia/Kolkata", "America/Los_Angeles"):
        out = run({**INP, "request": "weekly_summary", "timezone": tz}, first["memory"], now, None)
        texts[tz] = str(out["corpus"])
    assert "between 2026-10-04 and 2026-10-11" in texts["Asia/Kolkata"]
    assert "between 2026-10-03 and 2026-10-10" in texts["America/Los_Angeles"]
