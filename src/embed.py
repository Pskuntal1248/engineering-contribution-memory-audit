"""Embeddings through Gravity's LLM endpoint only (the official route). Used to group and link items by topic.
If the call fails or no endpoint exists, linking falls back to the TF-IDF similarity in linking.py."""
from __future__ import annotations

import asyncio
import math
from typing import Any

MODEL = "voyageai/voyage-4-lite"
BATCH = 64
MAX_BATCHES = 60        # bounded: at most ~3,800 items per run
TEXT_CHARS = 600


def item_text(it: Any) -> str:
    return (it.title + ". " + (it.text or "")[:TEXT_CHARS] + " " + " ".join(it.meta.get("paths", []) or [])).strip()


class GatewayEmbedder:
    def __init__(self, base_url: str, token: str, model: str = MODEL, timeout: float = 60.0):
        self.base_url, self.token, self.model, self.timeout = base_url, token, model, timeout
        self._e = None
        self.calls = 0

    def _emb(self):
        if self._e is None:
            from langchain_openai import OpenAIEmbeddings
            self._e = OpenAIEmbeddings(model=self.model, base_url=self.base_url, api_key=self.token, check_embedding_ctx_length=False, timeout=self.timeout, max_retries=1)
        return self._e

    async def embed(self, items: list[Any]) -> tuple[dict[str, list[float]], list[str]]:
        """Unit vectors keyed by item id, centred on the mean so unrelated items sit near zero. Notes say what went wrong."""
        vecs: dict[str, list[float]] = {}
        notes: list[str] = []
        sent = items[: BATCH * MAX_BATCHES]
        if len(items) > len(sent):
            notes.append(f"{len(items) - len(sent)} items not embedded (cap); grouped by keywords")
        sem = asyncio.Semaphore(4)
        failed: list[str] = []

        async def one(chunk: list[Any]) -> None:
            if failed:
                return
            try:
                async with sem:
                    self.calls += 1
                    out = await asyncio.wait_for(self._emb().aembed_documents([item_text(c) for c in chunk]), timeout=self.timeout + 10)
            except Exception as e:  # noqa: BLE001 - fall back to keyword similarity for everything
                failed.append(f"embeddings unavailable, grouped by keywords instead: {str(e)[:120]}")
                return
            if len(out) != len(chunk):
                failed.append("embeddings returned the wrong number of vectors, grouped by keywords instead")
                return
            for c, v in zip(chunk, out):
                vecs[c.id] = list(v)

        await asyncio.gather(*(one(sent[i : i + BATCH]) for i in range(0, len(sent), BATCH)))
        if failed:
            return {}, failed
        return center(vecs), notes


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
