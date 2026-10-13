"""Career-ladder handling. Expectations come from the framework text (bullet lines in code; prose read by the model with a quoted source span).
Which evidence shows each expectation is chosen by the model from the real item text; code validates every id, removes duplicates, counts items and
sets backed (3+) / thin (1-2) / missing (0). A model or budget failure is "unavailable", never "missing"."""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from guards import TRIVIAL_TITLE, BudgetExceeded
from metrics import status_for
from models import Item

_LEVEL = re.compile(r"^\s*(?:#+\s*|\*\*)?(?:level\s*:?\s*)?((?:senior\s+)?(?:software\s+)?(?:engineer|sde|swe)\s*(?:i{1,3}|iv|v|\d)?|staff\s+engineer|principal\s+engineer|se\s*(?:i{1,3}|iv|v|\d))\b", re.I)


# ----------------------------------------------------------------------------------------------------------- expectations
@dataclass
class Expectation:
    id: str
    level: str  # "current" | "next"
    text: str
    quote: str = ""  # the passage of the framework this expectation was read from (prose frameworks)


def parse_framework(text: str, current_level: str, next_level: str | None = None) -> list[Expectation]:
    """Bullet or numbered lines under a heading that names the current or next level.
    Headings are matched by level name; with no match every bullet counts as 'next'."""
    cur = re.sub(r"\W+", "", current_level.split(",")[0].lower())
    nxt = re.sub(r"\W+", "", (next_level or "").lower())
    section, out = "next", []
    bullets = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(#+|\*\*)?\s*(.*?)\s*(?:\*\*)?:?$", line)
        bullet = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)$", line)
        if bullet:
            bullets += 1
            t = bullet.group(1).strip().rstrip(".")
            out.append(Expectation(f"E{len(out)+1}", section, t))
        else:
            h = re.sub(r"\W+", "", line.lower())
            if cur and cur in h and re.search(r"\bto\b", line, re.I):
                section = "next"  # "Software Engineer to Senior Software Engineer expectations": what the next level asks
            elif cur and cur in h:
                section = "current"
            elif (nxt and nxt in h) or "nextlevel" in h or "promotion" in h:
                section = "next"
            elif _LEVEL.match(line):
                section = "next" if section == "current" else section
    return out


MAX_EXPECTATIONS = 40
WINDOW = 12000  # characters of framework text per extraction call
MAX_WINDOWS = 3
MIN_QUOTE_WORDS = 2  # a quote is a passage, not a single word


class ExtractedExpectation(BaseModel):
    text: str = Field(description="the expectation in one short line; it may paraphrase, but only what the quote supports")
    level: Literal["current", "next"] = Field(description="current = asked at the user's current level; next = asked at the next level up")
    source_quote: str = Field(description="a passage copied word for word from the framework text that states this expectation")


class ExtractedFramework(BaseModel):
    expectations: list[ExtractedExpectation]


def _norm(s: str) -> str:
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", s).strip().casefold()


def framework_hash(text: str, role_and_level: str) -> str:
    return hashlib.sha256(("v3\n" + role_and_level.strip().lower() + "\n" + text.strip()).encode()).hexdigest()[:16]


async def extract_with_model(llm, budget, framework: str, role_and_level: str) -> tuple[list[Expectation], str, int]:
    """Expectations from a framework written as prose. Each one must carry a source quote that really occurs in the framework text (whitespace, case
    and typographic quotes ignored); the wording of the expectation itself may be a supported paraphrase. Returns (expectations, reason, rejected):
    the reason is empty on success, `rejected` counts model lines dropped because their quote is not in the framework."""
    system = ("You read a company career ladder / review framework. List the expectations it states, each as one short line. For each, copy the passage "
              "that states it word for word into source_quote. level=current for expectations at the user's current level, level=next for the level above it; "
              "if the text does not separate levels, use next. Use ONLY the given text. Do not add, merge or invent expectations.")
    windows = [framework[i : i + WINDOW] for i in range(0, len(framework), WINDOW)]
    unread = max(0, len(windows) - MAX_WINDOWS)
    raw: list[ExtractedExpectation] = []
    first_error = ""
    for w in windows[:MAX_WINDOWS]:
        try:
            res = await llm.structured(ExtractedFramework, system, {"role_and_level": role_and_level, "framework_text": w}, budget)
            raw += list(getattr(res, "expectations", []))
        except BudgetExceeded as e:
            first_error = first_error or f"the model limit was reached before the framework could be read ({e})"
            break
        except Exception as e:  # noqa: BLE001
            first_error = first_error or f"the model call failed ({str(e)[:120]})"
            break
    if first_error and not raw:
        return [], first_error, 0
    haystack = _norm(framework)
    out: list[Expectation] = []
    seen: set[str] = set()
    rejected = 0
    for x in raw:
        q, t = _norm(x.source_quote), re.sub(r"\s+", " ", x.text).strip().rstrip(".")
        if not t or len(q.split()) < MIN_QUOTE_WORDS or q not in haystack:
            rejected += 1  # no passage of the framework backs it: invented
            continue
        if _norm(t) in seen:
            continue
        seen.add(_norm(t))
        out.append(Expectation(f"E{len(out)+1}", x.level, t, x.source_quote.strip()))
    notes = []
    if first_error:
        notes.append(f"only part of the framework was read: {first_error}")
    if unread:
        notes.append(f"the framework is longer than {MAX_WINDOWS * WINDOW} characters, so the last {unread} part(s) were not read")
    if len(out) > MAX_EXPECTATIONS:
        notes.append(f"{len(out)} expectations were found; only the first {MAX_EXPECTATIONS} are used")
        out = out[:MAX_EXPECTATIONS]
    if not out:
        return [], "the model returned no expectation whose source quote appears in the framework text" + (f" ({rejected} rejected)" if rejected else ""), rejected
    return out, "; ".join(notes), rejected


# ------------------------------------------------------------------------------------------------------------ evidence mapping
MAP_BATCH = 60       # evidence items per model request
MAX_MAP_CALLS = 40   # ceiling on mapping requests in one run; the run's remaining model budget (less a reserve) lowers it further
MAP_RESERVE = 4      # model calls left for the rest of the run
MAP_PARALLEL = 4
MAP_TEXT = 300       # characters of each item's text shown to the model
KEEP_PER_EXPECTATION = 25
DIGEST_TEXT = 160    # what a saved item still has (TEXT_KEEP), so a fresh item and its reloaded copy hash the same


class ExpectationPick(BaseModel):
    expectation_id: str
    item_ids: list[str] = Field(description="ids of the given items whose text shows the user doing this; empty if none does")


class EvidenceMap(BaseModel):
    picks: list[ExpectationPick]


def expectations_hash(exps: list[Expectation]) -> str:
    return hashlib.sha256("\n".join(f"{e.id}|{e.level}|{e.text}" for e in exps).encode()).hexdigest()[:16]


def mapping_candidates(items: list[Item]) -> list[Item]:
    """The user's own work that can count as evidence: not other people's items, not reverts or reverted PRs, not generated/lockfile-only PRs, not trivial commits."""
    out = []
    for i in items:
        m = i.meta
        if not (m.get("mine") or i.type == "review") or not i.title.strip():
            continue
        if m.get("reverted") or m.get("is_revert") or m.get("generated_only"):
            continue
        if i.type in {"commit", "pr"} and TRIVIAL_TITLE.search(i.title):
            continue
        out.append(i)
    return sorted(out, key=lambda i: (i.date, i.id), reverse=True)


def item_digest(i: Item) -> str:
    return hashlib.sha1(f"{i.id}|{i.type}|{i.date}|{i.title}|{i.text[:DIGEST_TEXT]}".encode()).hexdigest()[:12]


@dataclass
class Mapping:
    evidence: dict[str, list[str]] = field(default_factory=dict)
    incomplete: bool = False          # some candidate evidence was never shown to the model (failure, budget, or the per-run request cap)
    reasons: list[str] = field(default_factory=list)
    cache: dict[str, Any] | None = None
    new_calls: int = 0
    rejected: int = 0                 # ids the model returned that were not real (fabricated, or from another batch)
    reused: int = 0                   # candidate items answered from the saved mapping


async def map_evidence_with_model(llm, budget, exps: list[Expectation], items: list[Item], cache: dict[str, Any] | None) -> Mapping:
    res = Mapping()
    cands = mapping_candidates(items)
    digest = {c.id: item_digest(c) for c in cands}
    fw = expectations_hash(exps)
    cache = cache or {}
    if cache.get("fw") == fw:  # same expectations: keep the answers for items that have not changed
        done = {i: h for i, h in (cache.get("items") or {}).items() if digest.get(i) == h}
        emap = {e.id: [i for i in (cache.get("map") or {}).get(e.id, []) if i in done] for e in exps}
    else:
        done, emap = {}, {e.id: [] for e in exps}
    res.reused = len(done)
    todo = [c for c in cands if c.id not in done]
    valid_exps = {e.id for e in exps}
    exp_payload = [{"expectation_id": e.id, "level": e.level, "expectation": e.text} for e in exps]
    system = ("For each expectation, pick which of the given evidence items show the user doing it. Judge from each item's own title and text, not from its type or a "
              "shared word. An item may support several expectations, or none. Return only item_ids that appear in the given items and only expectation_ids from the given "
              "list; leave item_ids empty when no item shows it. Do not assess or compare people, and do not predict promotion.")
    if todo and llm is None:
        res.incomplete = True
        res.reasons.append("no model was available to map evidence to the expectations")
    elif todo:
        batches = [todo[i : i + MAP_BATCH] for i in range(0, len(todo), MAP_BATCH)]
        room = max(0, getattr(budget, "max_calls", MAX_MAP_CALLS + MAP_RESERVE) - getattr(budget, "calls", 0) - MAP_RESERVE)
        allowed = min(MAX_MAP_CALLS, room)
        gate = asyncio.Semaphore(MAP_PARALLEL)

        async def ask(chunk: list[Item]):
            payload = {"expectations": exp_payload, "items": [{"item_id": c.id, "type": c.type, "date": c.date[:10], "title": c.title, "text": c.text[:MAP_TEXT]} for c in chunk]}
            async with gate:
                try:
                    return await llm.structured(EvidenceMap, system, payload, budget), None
                except Exception as e:  # noqa: BLE001 - BudgetExceeded included: reported below, never read as "no evidence"
                    return None, e
        answers = await asyncio.gather(*(ask(c) for c in batches[:allowed]))
        for chunk, (got, err) in zip(batches[:allowed], answers):
            if err is not None:
                msg = (f"the model limit was reached while mapping evidence ({err})" if isinstance(err, BudgetExceeded) else f"a model call failed while mapping evidence ({str(err)[:120]})")
                if msg not in res.reasons:
                    res.reasons.append(msg)
                continue
            res.new_calls += 1
            real = {c.id for c in chunk}
            for p in getattr(got, "picks", []):
                if p.expectation_id not in valid_exps:
                    res.rejected += 1
                    continue
                for iid in p.item_ids:
                    if iid not in real:
                        res.rejected += 1  # fabricated, or an item from another request
                    elif iid not in emap[p.expectation_id]:
                        emap[p.expectation_id].append(iid)
            for c in chunk:
                done[c.id] = digest[c.id]
        if len(batches) > allowed:
            res.reasons.append(f"{sum(len(b) for b in batches[allowed:])} evidence items were not shown to the model this run (at most {allowed} mapping requests fit this run's model budget); they are mapped on the next run")
        res.incomplete = any(c.id not in done for c in todo)
    by_date = {c.id: c.date for c in cands}
    for k, v in emap.items():
        emap[k] = sorted(dict.fromkeys(v), key=lambda i: (by_date.get(i, ""), i), reverse=True)[:KEEP_PER_EXPECTATION]
    res.evidence = emap
    res.cache = {"fw": fw, "items": done, "map": emap}
    if res.rejected:
        res.reasons.append(f"{res.rejected} id(s) returned by the model were not real evidence ids and were ignored")
    return res


def pending_mapping_requests(exps: list[Expectation] | None, items: list[Item], cache: dict[str, Any] | None) -> int:
    """The mapping requests this run would actually send (same arithmetic as map_evidence_with_model, before the budget is applied). exps=None: the
    expectations are not read yet, so every candidate will be asked."""
    cands = mapping_candidates(items)
    todo = len(cands)
    if exps is not None and (cache or {}).get("fw") == expectations_hash(exps):
        have = (cache or {}).get("items") or {}
        todo = sum(1 for c in cands if have.get(c.id) != item_digest(c))
    return min(MAX_MAP_CALLS, -(-todo // MAP_BATCH))


def statuses(expectations: list[Expectation], evidence: dict[str, list[str]], known: set[str], incomplete: bool = False) -> list[dict]:
    """backed (3+ distinct valid items), thin (1-2), missing (none). Invalid ids never count. When the mapping is incomplete, a status that could change
    with the unseen evidence (thin or missing) is "unavailable": absence of a mapping is not evidence of absence. Three real items are backed either way."""
    rows = []
    for e in expectations:
        ids = [i for i in dict.fromkeys(evidence.get(e.id, [])) if i in known]
        st = status_for(ids)
        if incomplete and st != "backed":
            st = "unavailable"
        rows.append({"id": e.id, "level": e.level, "expectation": e.text, "status": st, "evidence": ids, "count": len(ids), "quote": e.quote})
    return rows
