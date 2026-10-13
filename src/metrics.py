"""Every number in every output comes from here. Deterministic: sorted with tie-breaks."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from guards import TRIVIAL_TITLE
from models import Item, day

# Review-feedback themes counted in code. The model may word the finding; it never counts it.
THEMES: dict[str, tuple[str, list[str]]] = {
    "tests": ("Missing or thin tests in PRs", [r"\bmissing tests?\b", r"\b(add|need|needs|where are|no)\b[^.\n]{0,25}\btests?\b", r"\btest coverage\b", r"\bunit tests?\b[^.\n]{0,20}\b(missing|needed|please)\b", r"\bcan we (get|add) (a )?tests?\b"]),
    "naming": ("Naming and readability comments", [r"\brename\b", r"\bnaming\b", r"\bunclear (name|variable)\b"]),
    "docs": ("Missing docs or comments", [r"\b(add|needs?|missing)\b[^.\n]{0,20}\b(docs?|documentation|docstring|comment)s?\b"]),
    "error_handling": ("Error handling gaps", [r"\berror handling\b", r"\bwhat happens (if|when)\b[^.\n]{0,40}\b(fails?|errors?|times? out)\b", r"\bhandle (the )?(failure|error|timeout)s?\b"]),
    "scope": ("PRs too large or mixed in scope", [r"\b(too (big|large)|split (this|it) (up|into)|separate pr)\b"]),
}
_COMPILED = {k: (label, [re.compile(p, re.I) for p in pats]) for k, (label, pats) in THEMES.items()}


def shipped(item: Item) -> bool:
    return item.type == "pr" and bool(item.meta.get("merged_flag")) and not item.meta.get("reverted") and not item.meta.get("is_revert") and not item.meta.get("generated_only") and not item.meta.get("docs_only")


def contribution_size(item: Item) -> int:
    return int((item.meta.get("size") or {}).get("lines", 0))


def top_contributions(items: list[Item], n: int = 10) -> list[Item]:
    prs = [i for i in items if shipped(i) and i.meta.get("mine")]
    return sorted(prs, key=lambda i: (-contribution_size(i), i.id))[:n]


def pr_stats(items: list[Item]) -> dict[str, Any]:
    mine = [i for i in items if i.type == "pr" and i.meta.get("mine")]
    cyc = [i.meta["cycle_hours"] for i in mine if i.meta.get("cycle_hours") is not None and shipped(i)]
    return {
        "prs_authored": len(mine),
        "prs_shipped": sum(1 for i in mine if shipped(i)),
        "commits_authored": sum(1 for i in items if i.type == "commit" and i.meta.get("mine") and not i.meta.get("is_revert") and not i.meta.get("reverted") and not TRIVIAL_TITLE.search(i.title)),
        "prs_reverted": sum(1 for i in mine if i.meta.get("reverted")),
        "median_cycle_hours": round(median(cyc), 1) if cyc else None,
        "reviews_given": sum(1 for i in items if i.type == "review" and i.meta.get("mine")),
        "tickets_touched": sum(1 for i in items if i.type == "ticket" and i.meta.get("mine")),
        "tickets_reopened": sum(1 for i in items if i.type == "ticket" and i.meta.get("mine") and i.meta.get("reopen_count", 0) > 0),
        "threads": sum(1 for i in items if i.type == "thread"),
        "docs": sum(1 for i in items if i.type == "doc" and i.meta.get("mine")),
    }


def review_feedback_themes(items: list[Item], min_instances: int = 3) -> list[dict[str, Any]]:
    """Feedback THE USER RECEIVED on their own PRs, counted per distinct PR. A theme needs >= min_instances."""
    hits: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for r in items:
        if r.type != "review" or not r.meta.get("received"):
            continue
        for key, (_label, pats) in _COMPILED.items():
            if any(p.search(r.text) for p in pats):
                hits[key][f"{r.meta['repo']}#{r.meta['number']}"].append(r.id)
    out = []
    for key, prs in hits.items():
        if len(prs) >= min_instances:
            ids = sorted(i for v in prs.values() for i in v)
            out.append({"theme": key, "label": _COMPILED[key][0], "count": len(prs), "prs": sorted(prs), "item_ids": ids})
    return sorted(out, key=lambda d: (-d["count"], d["theme"]))


def reopen_pattern(items: list[Item], min_instances: int = 3) -> dict[str, Any] | None:
    ids = sorted(i.id for i in items if i.type == "ticket" and i.meta.get("mine") and i.meta.get("reopen_count", 0) > 0)
    return {"theme": "reopened_tickets", "label": "Tickets reopened after Done", "count": len(ids), "item_ids": ids} if len(ids) >= min_instances else None


def slow_cycle_pattern(items: list[Item], factor: float = 2.0, min_instances: int = 3) -> dict[str, Any] | None:
    mine = [i for i in items if shipped(i) and i.meta.get("mine") and i.meta.get("cycle_hours") is not None]
    if len(mine) < 6:
        return None
    m = median(i.meta["cycle_hours"] for i in mine)
    slow = sorted(i.id for i in mine if i.meta["cycle_hours"] > factor * m and i.meta["cycle_hours"] > 48)
    return {"theme": "slow_cycle", "label": f"PRs open more than {factor:g}x the median cycle ({m:.0f}h)", "count": len(slow), "item_ids": slow} if len(slow) >= min_instances else None


def improvement_areas(items: list[Item]) -> list[dict[str, Any]]:
    out = review_feedback_themes(items)
    for extra in (reopen_pattern(items), slow_cycle_pattern(items)):
        if extra:
            out.append(extra)
    return sorted(out, key=lambda d: (-d["count"], d["theme"]))


def status_for(evidence_ids: list[str]) -> str:
    n = len(set(evidence_ids))
    return "backed" if n >= 3 else "thin" if n >= 1 else "missing"


def per_project_counts(items: list[Item], clusters: dict[str, dict]) -> dict[str, dict[str, int]]:
    by_id = {i.id: i for i in items}
    out = {}
    for pid, c in clusters.items():
        t = Counter(by_id[i].type for i in c["item_ids"])
        out[pid] = {"items": len(c["item_ids"]), **{k: t[k] for k in sorted(t)}}
    return out


def date_range(items: list[Item]) -> tuple[str, str]:
    ds = sorted(day(i) for i in items if i.date)
    return (ds[0], ds[-1]) if ds else ("", "")
