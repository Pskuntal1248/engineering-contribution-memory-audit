"""Hosted-style checkpointing: the platform checkpoints the WHOLE state after every step
(AGENT_STANDARD), so everything a node returns must be msgpack-serialisable plain data.
Local `gravity run`/`dev` attaches no checkpointer and can never catch this - these tests can.
They compile the real graph with a msgpack checkpointer and a LangGraph store, like the platform."""
import asyncio
import json
import warnings
from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import StateGraph
from langgraph.store.memory import InMemoryStore

import agent
import fixtures
from sources import FixtureSource

D = fixtures.build()
E = D["expected"]
INP = {"docs_source": "Confluence", "github_username": fixtures.USER, "repos_or_org": "acme/payments-api, acme/web", "jira_site": fixtures.SITE, "jira_projects": "PAY, WEB",
       "slack_channels": "#payments-eng, #incidents, #web-platform, #random", "include_dms": "No", "display_name": "Meera Joshi", "slack_user_ids": fixtures.ME,
       "role_and_level": "Software Engineer II", "review_framework": E["ladder_text"], "redact_terms": "Globex Foods, Initech Pay", "backfill_months": 6}
W1 = fixtures.START + timedelta(days=176)
W2 = fixtures.START + timedelta(days=190)


class Hosted:
    """Compile make_graph the way the platform runs it: checkpointed, one shared store per consumer+agent."""

    def __init__(self, with_store=True):
        self.store = InMemoryStore()
        self.with_store = with_store
        self._orig = StateGraph.compile
        this = self

        def patched(wf, **kw):
            kw.pop("checkpointer", None)
            kw.pop("store", None)
            if this.with_store:
                return this._orig(wf, checkpointer=InMemorySaver(), store=this.store, **kw)
            return this._orig(wf, checkpointer=InMemorySaver(), **kw)

        self._patched = patched

    def __enter__(self):
        StateGraph.compile = self._patched
        return self

    def __exit__(self, *a):
        StateGraph.compile = self._orig

    def run(self, inputs, memory, now, before, tid):
        deps = agent.Deps(lambda cfg: FixtureSource(D, before=before), llm=None, now=lambda: now)
        g = agent.make_graph(deps)
        return asyncio.run(g.ainvoke({"inputs": dict(inputs), "memory": memory}, {"configurable": {"thread_id": tid}}))


def test_backfill_weekly_and_resume_all_checkpointed():
    with Hosted() as h:
        first = h.run(INP, {}, W1, E["week2_start"], "cp1")
        assert first["result"]["mode"] == "backfill"
        second = h.run({**INP, "request": "auto"}, first["memory"], W2, None, "cp2")
        assert second["result"]["mode"] == "weekly" and second["result"]["new_items"] == 14
        third = h.run({**INP, "request": "auto"}, second["memory"], W2, None, "cp3")
        assert third["result"]["new_items"] == 0


@pytest.mark.parametrize("kind", ["weekly_summary", "one_on_one", "review_material", "interview_stories", "evidence_gaps"])
def test_on_demand_modes_checkpointed(kind):
    with Hosted() as h:
        first = h.run(INP, {}, W1, E["week2_start"], f"od-{kind}-a")
        out = h.run({**INP, "request": kind, "period_from": "2026-04-01", "period_to": "2026-09-30"}, first["memory"], W2, None, f"od-{kind}-b")
        assert out["result"]["mode"] == kind


def test_native_memory_path_checkpointed():
    with Hosted(with_store=False) as h:  # no platform store: state.memory carries the profile
        first = h.run(INP, {}, W1, E["week2_start"], "nv1")
        second = h.run({**INP, "request": "auto"}, first["memory"], W2, None, "nv2")
        assert first["result"]["mode"] == "backfill" and second["result"]["new_items"] == 14


def test_every_state_value_is_plain_msgpack_data():
    """The whole final state - ctx included - serialises as msgpack with no unregistered types.
    A live object (store, source, client) in state is what broke the hosted run."""
    serde = JsonPlusSerializer()
    with Hosted() as h:
        out = h.run(INP, {}, W1, E["week2_start"], "plain1")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # unregistered-type warnings become failures (strict msgpack blocks them next)
        for key, value in out.items():
            serde.loads_typed(serde.dumps_typed(value))
    ctx = out["ctx"]
    assert "store" not in ctx  # a store is a live connection; it is rebuilt inside nodes, never saved
    json.dumps(ctx["prior"])  # the saved history crossing node boundaries stays plain JSON


def test_missing_store_history_backfills_again_with_a_note_checkpointed():
    with Hosted() as h:
        first = h.run(INP, {}, W1, E["week2_start"], "ms1")
    with Hosted() as h2:  # a fresh, empty store behind an old marker
        out = h2.run(INP, first["memory"], W1, E["week2_start"], "ms2")
    assert out["result"]["mode"] == "backfill"
    assert any("missing from the store" in n for n in out["result"]["notes"])
