"""Plain data types shared by every module. No network, no model."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SOURCES = ("github", "jira", "slack", "doc")
# Item types. "pr", "commit", "review", "ticket", "thread", "doc".
TYPES = ("pr", "commit", "review", "ticket", "thread", "doc")


@dataclass
class Item:
    """One piece of evidence. `id` is stable across runs and is what every
    statement in the profile cites."""

    id: str
    source: str
    type: str
    date: str  # ISO 8601, UTC, e.g. 2026-03-04T10:15:00Z
    title: str
    url: str = ""
    text: str = ""
    author: str = ""
    co_authors: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # ticket keys mentioned
    refs: list[str] = field(default_factory=list)  # ids/urls of other items it points at
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Item":
        return Item(**{k: v for k, v in d.items() if k in Item.__dataclass_fields__})


@dataclass
class Skipped:
    """Something we read but did not keep, with the reason. Counted, never silent."""

    id: str
    reason: str


DEFAULT_TZ = "Asia/Kolkata"  # the manifest default; a blank input uses it and says so


def local_tz(name: str):
    """The user's timezone input as a tzinfo. Blank means the manifest default; a name that is not an IANA zone raises (inputs.py
    rejects it before a run starts) - there is no silent fallback to UTC."""
    from zoneinfo import ZoneInfo
    return ZoneInfo(str(name or "").strip() or DEFAULT_TZ)


def local_date_of(iso: str, tz) -> str:
    """Calendar day (YYYY-MM-DD) of a stored UTC ISO time in the user's timezone."""
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(tz).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return (iso or "")[:10]


def day(it: "Item") -> str:
    """The item's calendar day as the user sees it (their timezone); the stored date stays UTC for ordering and cursors."""
    return it.meta.get("local_date") or it.date[:10]
