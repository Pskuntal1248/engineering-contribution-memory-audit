"""Where the profile lives between runs. One interface, three backends.

NativeMemoryStore: state.memory (Gravity, <= state.max_kb). This is what ships.
LocalFileStore:    tests and local runs.
PlatformStore:     placeholder for get_store; NOT usable until Gravity confirms it exists for our agents.
The profile is compacted (and compressed) to fit, dropping the oldest detail first."""
from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path
from typing import Any, Protocol

SCHEMA = 1


class ProfileStore(Protocol):
    def load(self) -> dict[str, Any]: ...
    def dump(self, profile: dict[str, Any]) -> dict[str, Any]: ...


def pack(profile: dict[str, Any]) -> str:
    return base64.b64encode(zlib.compress(json.dumps(profile, separators=(",", ":"), sort_keys=True).encode(), 9)).decode()


def unpack(blob: str) -> dict[str, Any]:
    return json.loads(zlib.decompress(base64.b64decode(blob)).decode())


def packed_kb(profile: dict[str, Any]) -> float:
    return len(pack(profile)) / 1024


def fit(profile: dict[str, Any], max_kb: float) -> tuple[dict[str, Any], list[str]]:
    """Shrink until the packed profile fits. Order: item titles/urls of the oldest items first,
    then cached outputs, then oldest item rows. Seen-id keys are kept longest: they stop duplicates.
    Returns (profile, notes) where notes say exactly what was dropped."""
    p = json.loads(json.dumps(profile))
    notes: list[str] = []
    if packed_kb(p) <= max_kb:
        return p, notes
    items = sorted(p.get("items", []), key=lambda r: r.get("date", ""))
    for stage in ("title", "cached", "rows"):
        if stage == "title":
            for r in items:
                if packed_kb(p) <= max_kb:
                    break
                r["title"] = r.get("title", "")[:30]
                r.pop("url_extra", None)
            notes.append("shortened titles of older items")
        elif stage == "cached":
            if packed_kb(p) > max_kb and p.get("cached"):
                p["cached"] = {}
                notes.append("dropped cached on-demand outputs")
        else:
            dropped = 0
            while packed_kb(p) > max_kb and items:
                items.pop(0)
                p["items"] = items
                dropped += 1
                if dropped % 25 == 0:
                    continue
            if dropped:
                notes.append(f"dropped detail of the {dropped} oldest items (their ids are still remembered)")
        if packed_kb(p) <= max_kb:
            break
    p["items"] = items
    return p, notes


class NativeMemoryStore:
    def __init__(self, memory: dict[str, Any] | None, max_kb: int = 256, margin: float = 0.92):
        self.memory = memory or {}
        self.budget = max_kb * margin

    def load(self) -> dict[str, Any]:
        blob = self.memory.get("profile")
        if not blob:
            return {}
        try:
            p = unpack(blob)
            return p if p.get("schema") == SCHEMA else {}
        except Exception:  # corrupt memory: start fresh and say so upstream
            return {"_corrupt": True}

    def dump(self, profile: dict[str, Any]) -> dict[str, Any]:
        profile = dict(profile, schema=SCHEMA)
        p, notes = fit(profile, self.budget)
        p["compaction"] = notes
        # re-fit once more after adding notes
        p, _ = fit(p, self.budget)
        return {"profile": pack(p)}


class LocalFileStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def dump(self, profile: dict[str, Any]) -> dict[str, Any]:
        self.path.write_text(json.dumps(dict(profile, schema=SCHEMA), indent=1, sort_keys=True))
        return {"profile_path": str(self.path)}


class PlatformStore:
    """The full contribution history, in Gravity's get_store() (a LangGraph store, kept per user and agent).
    state.memory keeps only a small marker: backfill_done, cursors, last run date, evidence-gap statuses and counts.
    The store is written in `adump`, after the run's work succeeded; a re-run is safe because items are keyed by id."""

    NS = ("uc10", "profile")
    CHUNK = 150

    def __init__(self, store: Any, memory: dict[str, Any] | None):
        self.store, self.memory = store, memory or {}

    async def aload(self) -> dict[str, Any]:
        marker = self.memory.get("marker") or {}
        if not marker:
            return {}
        try:
            found = await self.store.asearch(self.NS, limit=1000)
        except Exception:  # noqa: BLE001
            return {"_corrupt": True}
        by_key = {it.key: it.value for it in found}
        items: list[dict[str, Any]] = []
        for k in sorted(k for k in by_key if k.startswith("items-")):
            items += by_key[k].get("rows", [])
        p: dict[str, Any] = {**marker, "items": items, "seen": by_key.get("seen", {}).get("ids", []), "verdicts": by_key.get("verdicts", {}).get("v", {}),
                          "cards": by_key.get("cards", {}).get("c", []), "dverdicts": by_key.get("dverdicts", {}).get("d", {})}
        if by_key.get("framework", {}).get("f"):
            p["framework"] = by_key["framework"]["f"]
        if by_key.get("mapping", {}).get("m"):
            p["mapping"] = by_key["mapping"]["m"]
        if marker.get("n_items", 0) and not items:  # marker says there was history, the store is empty: do not pretend
            p.update(backfill_done=False, last_run="", items=[], _store_empty=True)
        return p

    async def asave_framework(self, cache: dict[str, Any]) -> None:
        await self.store.aput(self.NS, "framework", {"f": cache})

    async def asave_mapping(self, cache: dict[str, Any]) -> None:
        await self.store.aput(self.NS, "mapping", {"m": cache})

    async def adump(self, profile: dict[str, Any]) -> dict[str, Any]:
        rows = profile.get("items", [])
        chunks = [rows[i : i + self.CHUNK] for i in range(0, len(rows), self.CHUNK)]
        for n, c in enumerate(chunks):
            await self.store.aput(self.NS, f"items-{n:04d}", {"rows": c})
        old = await self.store.asearch(self.NS, limit=1000)
        for it in old:  # chunks left over from a longer earlier list
            if it.key.startswith("items-") and int(it.key.split("-")[1]) >= len(chunks):
                await self.store.adelete(self.NS, it.key)
        await self.store.aput(self.NS, "seen", {"ids": profile.get("seen", [])})
        await self.store.aput(self.NS, "verdicts", {"v": profile.get("verdicts", {})})
        await self.store.aput(self.NS, "cards", {"c": profile.get("cards", [])})  # saved project names, goals, outcomes
        await self.store.aput(self.NS, "dverdicts", {"d": profile.get("dverdicts", {})})  # saved decision wording
        if profile.get("framework"):
            await self.store.aput(self.NS, "framework", {"f": profile["framework"]})  # prose framework -> expectations, by hash
        if profile.get("mapping"):
            await self.store.aput(self.NS, "mapping", {"m": profile["mapping"]})  # which evidence shows which expectation, by item content
        marker = {k: profile[k] for k in ("cursors", "last_run", "backfill_done", "gaps", "coverage", "repo_scope", "source_selection") if k in profile}
        marker.update(schema=SCHEMA, n_items=len(rows))
        return {"marker": marker}


def platform_store() -> Any | None:
    """The run's get_store(), or None when the platform gives none (then state.memory holds the profile)."""
    try:
        from langgraph.config import get_store
        return get_store()
    except Exception:  # noqa: BLE001
        return None
