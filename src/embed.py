"""Embeddings through Gravity's LLM endpoint only (the official route). Used to group and link items by topic.
Every network attempt is charged to the run's shared model-call Budget (the same one the chat calls use) before it is sent. The SDK's own retries
and request splitting are off, so an attempt in the log is an attempt on the wire. If the call fails, or there is no room, linking falls back to the
TF-IDF similarity in linking.py: embeddings are all-or-nothing for one linking pass, never a partial set."""
from __future__ import annotations

import asyncio
import math
from typing import Any

from guards import BudgetExceeded

MODEL = "voyageai/voyage-4-lite"
BATCH = 64
MAX_BATCHES = 60        # bounded: at most ~3,840 items in one pass
TEXT_CHARS = 600
RETRIES = 1             # one explicit retry per batch; each retry is charged like any other attempt
PARALLEL = 4


def item_text(it: Any) -> str:
    return (it.title + ". " + (it.text or "")[:TEXT_CHARS] + " " + " ".join(it.meta.get("paths", []) or [])).strip()


def batches_needed(n_items: int) -> int:
    return -(-n_items // BATCH)


class GatewayEmbedder:
    def __init__(self, base_url: str, token: str, model: str = MODEL, timeout: float = 60.0):
        self.base_url, self.token, self.model, self.timeout = base_url, token, model, timeout
        self._e = None
        self.calls = 0        # every attempt sent, over the embedder's life (failed and retried ones included)
        self.last: dict[str, Any] = {}

    def _emb(self):
        if self._e is None:
            from langchain_openai import OpenAIEmbeddings
            # max_retries=0: the SDK never re-sends on its own. chunk_size=BATCH: aembed_documents never splits a batch into more requests.
            self._e = OpenAIEmbeddings(model=self.model, base_url=self.base_url, api_key=self.token, check_embedding_ctx_length=False, timeout=self.timeout,
                                       max_retries=0, chunk_size=BATCH)
        return self._e

    async def embed(self, items: list[Any], budget: Any = None, reserve: int = 0) -> tuple[dict[str, list[float]], list[str]]:
        """Unit vectors keyed by item id, centred on the mean so unrelated items sit near zero - or nothing at all. Either every item is embedded
        in one pass (one centring) or the result is empty and the notes say why. `budget` is charged for each attempt just before it is sent;
        `reserve` calls are never touched. self.last reports the attempts."""
        stats: dict[str, Any] = {"items": len(items), "batches": batches_needed(len(items)), "attempts": 0, "failed_attempts": 0, "batches_ok": 0, "embedded": 0, "used": False, "stopped": ""}
        self.last = stats
        if not items:
            return {}, []
        if stats["batches"] > MAX_BATCHES:
            stats["stopped"] = "too many items"
            return {}, [f"embeddings not used: {len(items)} items need {stats['batches']} requests and one pass is limited to {MAX_BATCHES}; no request was sent, grouped by keywords and saved links"]
        vecs: dict[str, list[float]] = {}
        halt: list[str] = []
        sem = asyncio.Semaphore(PARALLEL)

        async def one(chunk: list[Any]) -> None:
            async with sem:
                last_err = ""
                for _ in range(RETRIES + 1):
                    if halt:
                        return  # something already ended this pass: queued batches send nothing
                    if budget is not None:
                        try:
                            budget.spend("embedding", reserve)  # check room and charge, with no await before the send below
                        except BudgetExceeded as e:
                            halt.append(f"the model budget ran out ({e})")
                            return
                    self.calls += 1
                    stats["attempts"] += 1
                    try:
                        out = await asyncio.wait_for(self._emb().aembed_documents([item_text(c) for c in chunk]), timeout=self.timeout + 10)
                    except Exception as e:  # noqa: BLE001
                        stats["failed_attempts"] += 1
                        last_err = f"endpoint error: {str(e)[:120]}"
                        continue
                    if len(out) != len(chunk):
                        stats["failed_attempts"] += 1
                        last_err = "the endpoint returned the wrong number of vectors"
                        continue
                    for c, v in zip(chunk, out):
                        vecs[c.id] = list(v)
                    stats["batches_ok"] += 1
                    return
                halt.append(last_err or "no attempt was possible")

        await asyncio.gather(*(one(items[i : i + BATCH]) for i in range(0, len(items), BATCH)))
        stats["embedded"] = len(vecs)
        if halt or len(vecs) != len(items):
            stats["stopped"] = halt[0] if halt else "incomplete"
            return {}, [f"embeddings not used: {stats['embedded']} of {len(items)} items were embedded in {stats['attempts']} request(s) ({stats['failed_attempts']} failed) before the pass "
                        f"stopped - {stats['stopped']}. A partly embedded set is not used, so grouping used keywords and saved links"]
        stats["used"] = True
        return center(vecs), []


def center(vecs: dict[str, list[float]]) -> dict[str, list[float]]:
    if not vecs:
        return {}
    dim = len(next(iter(vecs.values())))
    mean = [sum(v[d] for v in vecs.values()) / len(vecs) for d in range(dim)]
    out: dict[str, list[float]] = {}
    for k, v in vecs.items():
        c = [x - m for x, m in zip(v, mean)]
        n = math.sqrt(sum(x * x for x in c)) or 1.0
        out[k] = [x / n for x in c]
    return out


def dense_cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
