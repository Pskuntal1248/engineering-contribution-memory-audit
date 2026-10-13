"""Weekly increment: only items not seen before, with a cursor per source."""
from __future__ import annotations

from models import Item


def id_key(item_id: str) -> str:
    """Short stable key kept in memory instead of the whole id."""
    import hashlib
    return hashlib.blake2b(item_id.encode(), digest_size=6).hexdigest()


def new_items(items: list[Item], seen_keys: set[str]) -> tuple[list[Item], int]:
    """(items not in memory, number already seen). Also drops duplicates inside the batch."""
    fresh: list[Item] = []
    seen = set(seen_keys)
    skipped = 0
    for it in items:
        k = id_key(it.id)
        if k in seen:
            skipped += 1
            continue
        seen.add(k)
        fresh.append(it)
    return fresh, skipped


def next_cursors(cursors: dict[str, str], items: list[Item]) -> dict[str, str]:
    """Cursor per source = newest item date seen, with a 1-day overlap applied at fetch time."""
    out = dict(cursors)
    for it in items:
        if it.date and it.date > out.get(it.source, ""):
            out[it.source] = it.date
    return out


def since_for(cursor: str | None, backfill_start: str, overlap_days: int = 1) -> str:
    from datetime import datetime, timedelta
    if not cursor:
        return backfill_start
    d = datetime.fromisoformat(cursor.replace("Z", "+00:00")) - timedelta(days=overlap_days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def quiet_week_line(start: str, end: str) -> str:
    return f"No new work found between {start} and {end}."
