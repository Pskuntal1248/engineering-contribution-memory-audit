"""Separate current-run facts, optional omissions and material limits. Never display raw model errors."""
from __future__ import annotations
import re

FALLBACK = "Some analysis could not be completed. This output uses saved evidence and rules."


def split_notes(notes: list[str]) -> tuple[list[str], list[str], list[str]]:
    info, skipped, failures = [], [], []
    seen_cov: set[str] = set()
    for note in notes:
        low = note.lower()
        m = re.match(r"not covered:\s*([a-z]+)", low)
        if m:  # one 'not covered' line per source, whichever step wrote it
            if m.group(1) in seen_cov:
                continue
            seen_cov.add(m.group(1))
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
        elif low.startswith(("not covered:", "window:", "commit matching:", "github username taken", "jira site taken", "embeddings not re-run", "topic grouping used", "review framework: ", "timezone was not given", "model not used", "project names and goals from rules")) and "failed" not in re.sub(r"\b0 failed\b", "", low) and "not run" not in low:
            info.append(note)
        elif low.startswith("capped: notion: single"):
            continue
        else:
            failures.append(note)
    return tuple(list(dict.fromkeys(group)) for group in (info, skipped, failures))


_LABEL = {"github": "GitHub", "jira": "Jira", "slack": "Slack", "docs": "Docs"}


def coverage(asked: dict[str, bool], failed: list[dict], item_sources: dict[str, int], saved_only: bool = False, read_now: dict[str, bool] | None = None) -> tuple[str, dict[str, str]]:
    """What the profile covers, as one reader-facing line plus a status per source. The statuses are kept apart on purpose:
    'not requested' (the user gave nothing for it), 'not connected' (the tool refused for lack of a connection), 'could not be read'
    (any other failure) and 'partly read'. A saved-profile answer (no source read this run) reports only what the saved profile holds."""
    status: dict[str, str] = {}
    for key in ("github", "jira", "slack", "docs"):
        have = item_sources.get("doc" if key == "docs" else key, 0) > 0
        errs = [str(f.get("error", "")) for f in failed if str(f.get("source", "")).split(":")[0].split("-")[0] == key]
        if saved_only:
            status[key] = "in the saved profile" if have else "not in the saved profile"
        elif not asked.get(key):
            status[key] = "not requested"
        elif errs and have and read_now is not None and not read_now.get(key, True):  # saved items exist, but nothing was read this run
            status[key] = "could not be read"
        elif errs and have:
            status[key] = "partly read"
        elif errs:
            status[key] = "not connected" if any("not connected" in e.lower() for e in errs) else "could not be read"
        else:
            status[key] = "read"
    good = [k for k, v in status.items() if v in {"read", "partly read", "in the saved profile"}]
    names = [_LABEL[k] for k in good]
    head = ("Coverage: " + (f"{names[0]} only" if len(names) == 1 else ", ".join(names) if names else "nothing was read"))
    rest = [f"{_LABEL[k]} {v}" for k, v in status.items() if k not in good]
    partial = [f"{_LABEL[k]} only partly read" for k, v in status.items() if v == "partly read"]
    stale = [f"{_LABEL[k]} could not be read this run; earlier saved {_LABEL[k]} work is still in the profile" for k, v in status.items() if v == "could not be read" and item_sources.get("doc" if k == "docs" else k, 0) > 0 and not saved_only]
    line = head + (" (" + "; ".join(rest + partial + stale) + ")" if rest or partial or stale else "") + "."
    return line, status
