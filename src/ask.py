"""Team changes typed one per line ("2026-07-01: Platform team"). Pure code, no model."""
from __future__ import annotations

import re


def parse_team_changes(text: str) -> list[tuple[str, str]]:
    """Lines like "2026-07-01: Platform team" -> [("2026-07-01", "Platform team")], sorted by date."""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s*[:\-,]\s*(.+?)\s*$", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return sorted(out)
