"""Embedding budget enforcement, with the real GatewayEmbedder (real OpenAIEmbeddings client, real HTTP) talking to a local OpenAI-style /v1/embeddings
endpoint, wired through agent -> eprofile.build. LOCAL mocks only: no paid or live call, and nothing here is the official samples/UC-10 acceptance."""
import asyncio
import base64
import hashlib
import json
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import agent
import embed
import ladder
from embed import GatewayEmbedder
from guards import MODE_LIMITS, Budget
from models import Item
from store import PlatformStore
from test_round2 import FrameworkLLM, PROSE

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


class MockEmbeddingsEndpoint:
    """POST /v1/embeddings like the OpenAI API (float or base64 encoding). Every HTTP request is recorded; behaviours are programmable."""

    def __init__(self, delay=0.0, fail_requests=(), fail_all=False, probe=None):
        self.delay, self.fail_requests, self.fail_all, self.probe = delay, set(fail_requests), fail_all, probe
        self.requests: list[dict] = []   # {"n": inputs in the request, "t": start time}
        self._lock, self._live, self.peak = threading.Lock(), 0, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                inputs = body.get("input") or []
                inputs = [inputs] if isinstance(inputs, str) else inputs
                with outer._lock:
                    outer.requests.append({"n": len(inputs), "t": time.monotonic(), "seen": outer.probe() if outer.probe else None})
                    idx = len(outer.requests)
                    outer._live += 1
                    outer.peak = max(outer.peak, outer._live)
                try:
                    time.sleep(outer.delay)
                    if outer.fail_all or idx in outer.fail_requests:
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error": {"message": "boom", "type": "server_error"}}')
                        return
                    data = []
                    for i, text in enumerate(inputs):
                        raw = hashlib.md5(str(text).encode()).digest()[:16]
                        vec = [(b - 127.5) / 127.5 for b in raw[:8]]
                        emb = base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode() if body.get("encoding_format") == "base64" else vec
                        data.append({"object": "embedding", "index": i, "embedding": emb})
                    out = json.dumps({"object": "list", "data": data, "model": body.get("model", "m"), "usage": {"prompt_tokens": 1, "total_tokens": 1}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                finally:
                    with outer._lock:
                        outer._live -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @property
    def attempts(self) -> int:
        return len(self.requests)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    made = []

    def make(**kw):
        e = MockEmbeddingsEndpoint(**kw)
        made.append(e)
        return e
    yield make
    for e in made:
        e.stop()


def items(n, start=0):
    return [Item(id=f"i{start + k}", source="github", type="pr", date="2026-09-10T00:00:00Z", title=f"Retry handling number {start + k}", text=f"worked on batch {start + k}", meta={"mine": True}) for k in range(n)]


def embedder(ep, timeout=5.0):
    return GatewayEmbedder(ep.url, "test-token", timeout=timeout)


def run_embed(e, n, budget=None, reserve=0):
    return asyncio.run(e.embed(items(n), budget=budget, reserve=reserve))


# --------------------------------------------------------------------------------------------- the wire matches the charges
def test_one_attempt_in_the_log_is_one_request_on_the_wire_and_sdk_batching_adds_none(endpoint):
    ep = endpoint()
    b = Budget(max_calls=50, max_seconds=100)
    e = embedder(ep)
    vecs, notes = run_embed(e, 200, b)                         # 4 batches: 64+64+64+8
    assert len(vecs) == 200 and notes == [] and e.last["used"]
    assert ep.attempts == 4 == b.calls == b.by_kind["embedding"] == e.calls == e.last["attempts"]
    assert sorted(r["n"] for r in ep.requests) == [8, 64, 64, 64] and ep.peak <= embed.PARALLEL


def test_the_sdk_does_not_retry_or_split_on_its_own(endpoint):
    ep = endpoint(fail_all=True)
    e = embedder(ep)
    with pytest.raises(Exception):
        asyncio.run(e._emb().aembed_documents(["a"]))
    assert ep.attempts == 1                                    # max_retries=0: a failed call is one request, not two or three
    ep2 = endpoint()
    e2 = embedder(ep2)
    asyncio.run(e2._emb().aembed_documents([f"t{k}" for k in range(130)]))
    assert [r["n"] for r in ep2.requests] == [64, 64, 2]       # chunk_size=BATCH: if misused, the SDK would split at 64, never send more; embed() itself never passes more than 64


def test_every_attempt_is_charged_before_it_reaches_the_network(endpoint):
    box = {}
    ep = endpoint(delay=0.05, probe=lambda: box["b"].calls)                    # what the budget says at the moment each request arrives
    box["b"] = Budget(max_calls=50, max_seconds=100)
    run_embed(embedder(ep), 64 * 12, box["b"])
    seen = sorted(r["seen"] for r in ep.requests)
    assert seen == sorted(seen) and all(seen[k] >= k + 1 for k in range(len(seen)))   # the k-th request was already paid for when it arrived: no uncharged send, no charge-after-send


def test_failed_attempts_and_the_explicit_retry_are_charged(endpoint):
    ep = endpoint(fail_requests={1})                           # the first request fails; its explicit retry succeeds
    b = Budget(max_calls=50, max_seconds=100)
    e = embedder(ep)
    vecs, notes = run_embed(e, 64 * 3, b)
    assert len(vecs) == 192 and ep.attempts == 4 == b.calls == e.calls and e.last["failed_attempts"] == 1
    ep = endpoint(fail_all=True)
    b = Budget(max_calls=50, max_seconds=100)
    e = embedder(ep)
    vecs, notes = run_embed(e, 64 * 8, b)
    assert vecs == {} and "not used" in notes[0] and "keywords" in notes[0]
    assert ep.attempts == b.calls == e.calls == e.last["attempts"] and e.last["failed_attempts"] == ep.attempts       # every failed attempt counted, none uncounted
    assert ep.attempts <= (embed.PARALLEL) * (embed.RETRIES + 1)                                                        # queued batches stopped once the pass was dead


# --------------------------------------------------------------------------------------------- exhaustion and queued batches
def test_an_exhausted_or_pre_spent_budget_sends_nothing(endpoint):
    ep = endpoint()
    b = Budget(max_calls=10, max_seconds=100, calls=10)
    vecs, notes = run_embed(embedder(ep), 700, b)
    assert vecs == {} and ep.attempts == 0 and b.calls == 10 and "budget" in notes[0] and "0 of 700" in notes[0]
    b = Budget(max_calls=10, max_seconds=100, calls=7)         # three calls left, but 11 batches wanted
    ep = endpoint()
    vecs, notes = run_embed(embedder(ep), 700, b)
    assert vecs == {} and ep.attempts == 3 == b.calls - 7 and b.calls == 10 and "3 request(s)" in notes[0] and "192 of 700" in notes[0]


def test_queued_batches_stop_when_the_call_room_is_gone_even_with_concurrency(endpoint):
    ep = endpoint(delay=0.15)
    b = Budget(max_calls=20, max_seconds=100, calls=15)         # room for exactly 5 attempts; 30 batches queued behind a semaphore of 4
    vecs, notes = run_embed(embedder(ep), 64 * 30, b)
    assert vecs == {} and ep.attempts == 5 and b.calls == 20 and ep.peak <= embed.PARALLEL


def test_the_reserve_is_never_touched(endpoint):
    ep = endpoint()
    b = Budget(max_calls=10, max_seconds=100)
    vecs, notes = run_embed(embedder(ep), 64 * 9, b, reserve=4)   # 10 - 4 = 6 usable, 9 wanted
    assert vecs == {} and ep.attempts == 6 and b.calls == 6 and b.room() == 4 and "kept for work still pending" in notes[0]


def test_time_exhaustion_stops_queued_batches(endpoint):
    ep = endpoint(delay=0.25)
    b = Budget(max_calls=100, max_seconds=0.4)
    vecs, notes = run_embed(embedder(ep), 64 * 20, b)
    assert vecs == {} and 0 < ep.attempts < 20 and ep.attempts == b.calls and "time limit" in notes[0]
    ep = endpoint()
    b = Budget(max_calls=100, max_seconds=0.001)
    time.sleep(0.01)
    vecs, notes = run_embed(embedder(ep), 700, b)
    assert vecs == {} and ep.attempts == 0 and "time limit" in notes[0]


# --------------------------------------------------------------------------------------------- through agent -> eprofile.build
def raw_docs(n, start=0):
    docs = []
    for k in range(start, start + n):
        docs.append({"_source": "drive", "id": f"d{k}", "title": f"Notes {k}: tuning job {k % 37}", "labels": [], "author": "Me", "editors": ["Me"], "created": "2026-10-01T00:00:00Z",
                     "modified": "2026-10-02T00:00:00Z", "url": f"https://docs.google.com/document/d/d{k}/edit", "kind": "google doc", "_mine": True, "body_failed": False,
                     "body": f"Worked through the tuning of job {k % 37} on batch {k}; wrote down what changed and what to watch next time. Reference {k}.", "_attribution": "edit evidence"})
    return docs


class Docs:
    def __init__(self, n):
        self.n = n

    async def fetch(self, since, until, log):
        return {"github": {"prs": [], "reviews": {}, "commits": []}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": raw_docs(self.n), "failed": [], "capped": []}


def go(inputs, store, n_docs, ep=None, llm=None, request=None, now=NOW, embedder_obj=None):
    emb = embedder_obj or (embedder(ep) if ep else None)
    deps = agent.Deps(lambda cfg: Docs(n_docs), llm=llm, now=lambda: now, embedder=emb, store_factory=lambda mem: PlatformStore(store, mem))
    base = {"github_username": "u", "repos_or_org": "o/r", "role_and_level": "Software Engineer II", "jira_projects": "PAY", "docs_source": "Google Drive", **inputs}
    if request:
        base["request"] = request
    return asyncio.run(agent.make_graph(deps).ainvoke({"inputs": base, "memory": go.mem.get(id(store), {})})), emb


go.mem = {}


def run(inputs, store, n_docs, **kw):
    out, emb = go(inputs, store, n_docs, **kw)
    if out.get("memory"):
        go.mem[id(store)] = out["memory"]
    return out, emb


def new_store():
    from langgraph.store.memory import InMemoryStore
    return InMemoryStore()


def check_totals(out, ep, mode):
    r = out["result"]
    limit_calls, _ = MODE_LIMITS[mode]
    assert r["model_calls"] == r["embedding_calls"] + r["chat_calls"] <= limit_calls
    assert r["embedding_calls"] == ep.attempts if ep is not None else True      # reported == attempts on the wire


def test_backfill_embeds_everything_within_the_shared_limit_and_reports_attempts_exactly(endpoint):
    from langgraph.store.memory import InMemoryStore
    ep, store = endpoint(), InMemoryStore()
    out, e = run({}, store, 700, ep=ep)
    r = out["result"]
    assert r["mode"] == "backfill" and ep.attempts == embed.batches_needed(700) == 11 == r["embedding_calls"] and r["model_calls"] == 11 + r["chat_calls"] <= 150
    assert any("topic grouping used embeddings: all 700 items in 11 request(s), 0 failed" in n for n in r["notes"])


def test_on_demand_700_items_sends_no_embedding_request_and_says_why(endpoint):
    ep, store = endpoint(), new_store()
    run({}, store, 700, ep=ep)                                   # backfill
    before = ep.attempts
    llm = FrameworkLLM()
    out, _ = run({"review_framework": PROSE}, store, 0, ep=ep, llm=llm, request="evidence_gaps", now=NOW + timedelta(days=3))
    r = out["result"]
    assert ep.attempts == before and r["embedding_calls"] == 0                                      # 11 embedding requests would have exceeded the whole 10-call on-demand limit
    assert r["model_calls"] == r["chat_calls"] <= MODE_LIMITS["on_demand"][0] and any("embeddings not re-run" in n for n in r["notes"])
    assert llm.n == 1 and llm.n_map >= 1                                                           # framework reading and evidence mapping got their calls


def test_weekly_with_new_evidence_embeds_all_when_it_fits_and_never_exceeds_the_limit(endpoint):
    ep, store = endpoint(), new_store()
    run({}, store, 400, ep=ep)
    base = ep.attempts
    out, _ = run({}, store, 414, ep=ep, now=NOW + timedelta(days=7))          # 14 new items
    r = out["result"]
    used = ep.attempts - base
    assert r["mode"] == "weekly" and r["new_items"] == 14 and used == embed.batches_needed(414) == 7 == r["embedding_calls"] and r["model_calls"] <= MODE_LIMITS["weekly"][0]


@pytest.mark.parametrize("n_items", [1700, 3800])
def test_weekly_on_a_big_history_skips_embeddings_instead_of_overrunning(endpoint, n_items):
    ep, store = endpoint(), new_store()
    run({}, store, n_items - 20, ep=None)                                      # backfill without embeddings (keeps the setup cheap)
    out, _ = run({}, store, n_items, ep=ep, now=NOW + timedelta(days=7))        # 20 new items on a big history
    r = out["result"]
    assert r["mode"] == "weekly" and r["new_items"] == 20 and ep.attempts == 0 and r["embedding_calls"] == 0 and r["model_calls"] <= MODE_LIMITS["weekly"][0]
    note = " ".join(r["notes"])
    assert "embeddings not used" in note and "No embedding request was sent" in note and "keywords and the saved project links" in note


def test_mapping_keeps_its_room_when_embeddings_would_have_eaten_it(endpoint):
    ep, store = endpoint(), new_store()
    run({}, store, 880, ep=None)                                                    # backfill without a framework: nothing is mapped or cached yet
    llm = FrameworkLLM()
    out, _ = run({"review_framework": PROSE}, store, 900, ep=ep, llm=llm, now=NOW + timedelta(days=7))   # weekly: the framework arrives now; 20 new items
    r = out["result"]
    # pending chat work = 1 framework reading + 15 mapping requests (900 items / 60) + the reserve of 4 = 20 of the 25 calls, so a 15-request embedding pass does not fit
    assert r["mode"] == "weekly" and ep.attempts == 0 and r["embedding_calls"] == 0
    assert "No embedding request was sent" in " ".join(r["notes"]) and "evidence mapping 15" in " ".join(r["notes"])
    assert llm.n == 1 and llm.n_map == 15 and not any(g["status"] == "unavailable" for g in r["evidence_gaps"])   # the mapping completed with the calls that were kept
    assert r["model_calls"] == 16 <= MODE_LIMITS["weekly"][0]


def test_when_embeddings_fit_the_mapping_still_gets_all_its_requests(endpoint):
    ep, store = endpoint(), new_store()
    run({}, store, 300, ep=None)
    llm = FrameworkLLM()
    out, _ = run({"review_framework": PROSE}, store, 320, ep=ep, llm=llm, now=NOW + timedelta(days=7))      # 5 embedding requests (320/64) + 1 reading + 6 mapping requests (320/60)
    r = out["result"]
    assert ep.attempts == 5 == r["embedding_calls"] and llm.n == 1 and llm.n_map == 6 and not any(g["status"] == "unavailable" for g in r["evidence_gaps"])
    assert r["model_calls"] == 12 <= MODE_LIMITS["weekly"][0]


def test_unfinished_mapping_is_unavailable_never_missing_and_never_called_complete(endpoint):
    ep, store = endpoint(), new_store()
    llm = FrameworkLLM()
    out, _ = run({"review_framework": PROSE}, store, 1700, ep=ep, llm=llm, request=None, now=NOW)       # backfill of 1,700 items: mapping needs 29 requests
    r = out["result"]
    assert r["mode"] == "backfill" and r["model_calls"] <= 150
    # a cold on-demand request has 10 calls: it can map only part of 1,700 items and must say so
    llm2 = FrameworkLLM(fail_mapping=False)
    store2 = new_store()
    run({}, store2, 1700, ep=None)
    wire_before = ep.attempts
    out, _ = run({"review_framework": PROSE}, store2, 0, ep=ep, llm=llm2, request="evidence_gaps", now=NOW + timedelta(days=2))
    r = out["result"]
    assert r["model_calls"] <= MODE_LIMITS["on_demand"][0] and r["embedding_calls"] == 0 and ep.attempts == wire_before
    gaps = r["evidence_gaps"]
    assert gaps and any(g["status"] == "unavailable" for g in gaps) and not any(g["status"] == "missing" for g in gaps if g["status"] != "backed")
    note = " ".join(r["notes"])
    assert "evidence mapping UNAVAILABLE" in note and "not a finding that evidence is missing" in note and "mapped on the next run" in note


def test_failures_are_reported_and_fall_back_to_keywords_without_breaking_the_run(endpoint):
    ep, store = endpoint(fail_all=True), new_store()
    out, _ = run({}, store, 300, ep=ep)
    r = out["result"]
    assert r["status"] == "completed" and ep.attempts == r["embedding_calls"] >= 1 and r["model_calls"] == r["chat_calls"] + r["embedding_calls"] <= 150
    assert any("embeddings not used" in n and "keywords and saved links" in n and "failed" in n for n in r["notes"])
    assert not any("topic grouping used embeddings" in n for n in r["notes"])


def test_repeated_runs_do_not_re_embed_unchanged_history_and_changed_inputs_embed_again(endpoint):
    ep, store = endpoint(), new_store()
    run({}, store, 200, ep=ep)
    a = ep.attempts
    out, _ = run({}, store, 200, ep=ep, now=NOW + timedelta(days=7))          # nothing new
    assert ep.attempts == a and out["result"]["new_items"] == 0 and out["result"]["embedding_calls"] == 0
    assert any("no new evidence" in n for n in out["result"]["notes"])
    out, _ = run({}, store, 230, ep=ep, now=NOW + timedelta(days=14))         # 30 new items: the whole pass runs again, centred once
    assert ep.attempts - a == embed.batches_needed(230) and out["result"]["embedding_calls"] == embed.batches_needed(230)
    saved = json.dumps([it.value for it in asyncio.run(store.asearch(PlatformStore.NS, limit=1000))])
    assert "embedding" not in saved and "vector" not in saved                   # nothing like a vector cache is saved: a saved item keeps truncated text, so an old vector could not be matched to its input


@pytest.mark.parametrize("mode,limit", [("backfill", 150), ("weekly", 25), ("on_demand", 10)])
def test_embedding_and_chat_share_one_limit_in_every_mode(endpoint, mode, limit):
    ep, store = endpoint(), new_store()
    llm = FrameworkLLM()
    inputs = {"review_framework": PROSE}
    if mode == "backfill":
        out, _ = run(inputs, store, 300, ep=ep, llm=llm)
    else:
        run(inputs, store, 300, ep=ep, llm=llm)
        out, _ = run(inputs, store, 330 if mode == "weekly" else 0, ep=ep, llm=llm, request=None if mode == "weekly" else "evidence_gaps", now=NOW + timedelta(days=7))
    r = out["result"]
    assert MODE_LIMITS[mode][0] == limit
    assert r["model_calls"] == r["embedding_calls"] + r["chat_calls"] <= limit
    assert r["embedding_calls"] == (ep.attempts - (embed.batches_needed(300) if mode != "backfill" else 0))
