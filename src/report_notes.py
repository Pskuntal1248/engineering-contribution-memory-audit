"""Separate current-run facts, optional omissions and material limits. Never display raw model errors."""
from __future__ import annotations
import re

FALLBACK = "Some analysis could not be completed. This output uses saved evidence and rules."


def split_notes(notes: list[str]) -> tuple[list[str], list[str], list[str]]:
    info, skipped, failures = [], [], []
    for note in notes:
        low = note.lower()
        if low.startswith("evidence mapping unavailable"):
            clean = re.sub(r"(?:model call failed|model analysis unavailable|model analysis could not)[^;]*", "analysis unavailable", note, flags=re.I)
            failures.append(clean)
            failures.append(FALLBACK)
        elif low.startswith("review framework:") and ("model call failed" in low or "model analysis" in low):
            failures.extend(["Review framework analysis could not be completed. Evidence gaps were NOT run.", FALLBACK])
        elif low.startswith(("model call failed", "model analysis")) or "model call failed" in low or "model analysis unavailable" in low or "model analysis could not" in low:
            failures.append(FALLBACK)
        elif low.startswith("no review framework given"):
            skipped.append("no review framework given: evidence gaps skipped; a framework would enable them")
        elif low.startswith(("not covered:", "github username taken", "jira site taken", "embeddings not re-run", "topic grouping used", "review framework: ", "timezone was not given", "model not used", "project names and goals from rules")) and "failed" not in low and "not run" not in low:
            info.append(note)
        elif low.startswith("capped: notion: single"):
            continue
        else:
            failures.append(note)
    return tuple(list(dict.fromkeys(group)) for group in (info, skipped, failures))
