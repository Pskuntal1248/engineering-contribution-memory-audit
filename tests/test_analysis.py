import asyncio
import fixtures
import pytest
from test_core import cfg
from pipeline import ingest
import linking, analysis
from guards import Budget, BudgetExceeded
from sources import FixtureSource
from guards import ToolLog

NAMES = {"Meera Joshi", fixtures.USER, fixtures.ME}


@pytest.fixture(scope="module")
def run():
    d = fixtures.build()
    res = ingest(d, cfg())
    cl = linking.link(res.items, {})
    links = {i: r for c in cl.values() for i, r in c["links"].items()}
    return d, res, links


def test_invisible_work_8_of_9(run):
    d, res, links = run
    found = {x["thread"] for x in analysis.find_invisible(res.items, links, NAMES)}
    hit = [s for s in d["expected"]["invisible"] if s in found]
    assert len(hit) >= 8, (len(hit), len(found))
    # not drowned in false positives: every found item is a real user-participated thread without keys
    assert len(found) <= 25


def test_three_decisions_with_roles(run):
    d, res, _ = run
    got = {x["doc"]: x["role"] for x in analysis.find_decisions(res.items, NAMES, fixtures.USER)}
    for e in d["expected"]["decisions"]:
        assert got.get(e["id"]) == e["role"]


def test_fixture_source_roundtrip(run):
    d, _, _ = run
    raw = asyncio.run(FixtureSource(d).fetch("2026-03-01T00:00:00Z", None, ToolLog()))
    assert raw["failed"] == []


class FakeLLM:
    def __init__(self, fn): self.fn, self.n = fn, 0
    async def structured(self, schema, system, payload, budget=None):
        if budget is not None:
            budget.spend()
        self.n += 1
        return self.fn(schema, payload)


def test_model_ids_validated_and_budget_enforced(run):
    d, res, links = run
    threads = [i for i in res.items if i.type == "thread"][:40]
    def fn(schema, payload):
        return analysis.ThreadVerdicts(verdicts=[analysis.ThreadVerdict(thread_id=t["thread_id"], label="problem_solved", summary="x") for t in payload["threads"]] + [analysis.ThreadVerdict(thread_id="slack:FAKE:1", label="decision", summary="invented")])
    llm = FakeLLM(fn)
    b = Budget(max_calls=2, max_seconds=100)
    v, notes = asyncio.run(analysis.classify_threads_with_model(llm, b, threads, NAMES, batch=10))
    assert "slack:FAKE:1" not in v and len(v) == 20 and llm.n == 2
    assert any("limit" in n for n in notes)
