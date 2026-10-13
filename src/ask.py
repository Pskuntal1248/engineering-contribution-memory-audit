"""On-demand requests typed as words: "weekly summary", "1:1 points", "review material for April to September",
"interview stories", "evidence gaps". Pure code, no model. Returns (mode, date_from, date_to); mode "" means not recognised."""
from __future__ import annotations

import calendar
import re
from datetime import date

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))

_MODES = (
    ("interview_stories", r"interview"),
    ("evidence_gaps", r"evidence gaps?|gaps?\b|next level|promotion"),
    ("review_material", r"review material|self.?review|performance review|review\b"),
    ("one_on_one", r"1\s*[:\-]\s*1|one.on.one|1on1|talking points"),
    ("weekly_summary", r"weekly|this week|summary"),
)


def parse_request(text: str, today: date) -> tuple[str, str, str]:
    t = (text or "").strip().lower()
    if not t:
        return "", "", ""
    mode = next((m for m, rx in _MODES if re.search(rx, t)), "")
    d_from = d_to = ""
    iso = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if len(iso) >= 2:
        d_from, d_to = sorted(iso)[0], sorted(iso)[-1]
    else:
        m = re.search(rf"\b({_MON})\w*\.?(?:\s+(\d{{4}}))?\s*(?:to|-|until|through|till)\s*({_MON})\w*\.?(?:\s+(\d{{4}}))?", t)
        if m:
            m1, y1, m2, y2 = _MONTHS[m.group(1)[:3] if m.group(1)[:3] in _MONTHS else m.group(1)], m.group(2), _MONTHS[m.group(3)[:3] if m.group(3)[:3] in _MONTHS else m.group(3)], m.group(4)
            y_end = int(y2) if y2 else today.year
            if not y2 and (y_end, m2) > (today.year, today.month):
                y_end -= 1  # a range that would end in the future means the most recent past one
            y_start = int(y1) if y1 else (y_end - 1 if m1 > m2 else y_end)
            d_from = f"{y_start:04d}-{m1:02d}-01"
            d_to = f"{y_end:04d}-{m2:02d}-{calendar.monthrange(y_end, m2)[1]:02d}"
    return mode, d_from, d_to


def parse_team_changes(text: str) -> list[tuple[str, str]]:
    """Lines like "2026-07-01: Platform team" -> [("2026-07-01", "Platform team")], sorted by date."""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s*[:\-,]\s*(.+?)\s*$", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return sorted(out)
