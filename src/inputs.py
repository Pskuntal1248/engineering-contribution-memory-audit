"""Input checks that run before any source is read or any model is called. Every problem is collected and reported in one pass,
so a bad value stops the run with an explicit message instead of being guessed at (or crashing later with a bare ValueError/KeyError).
github_username and jira_site may be blank: the run fills them from the connected accounts."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

REQUESTS = ("weekly_update", "weekly_summary", "one_on_one", "review_material", "interview_stories", "evidence_gaps")
DOCS_PARTS = {"none", "confluence", "notion", "google drive"}
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")  # a bare GitHub organisation or user name
_TEAM_LINE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*[:\-,]\s*(.+?)\s*$")


def _blank(v: Any) -> bool:
    return v is None or str(v).strip() == ""


def _iso(v: str) -> date | None:
    try:
        return date.fromisoformat(v.strip()) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()) else None
    except ValueError:
        return None


def parse_months(v: Any) -> int | None:
    """A positive whole number, as a number or as text. No upper limit. Anything else is None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, float):
        n = int(v) if v == int(v) else 0
    else:
        s = str(v).strip()
        m = re.fullmatch(r"\+?(\d+)(?:\.0+)?", s)
        n = int(m.group(1)) if m else 0
    return n if n >= 1 else None


def backfill_since(now: datetime, months: int) -> str:
    """The start of the backfill window as YYYY-MM-DDTHH:MM:SSZ: `months` x 30 days before `now`. If that date cannot be represented the request is refused with a
    clear error - it is never shortened to a nearer date and ingested as if it were the whole period."""
    floor = datetime(1, 1, 1, tzinfo=timezone.utc)
    try:
        start = now - timedelta(days=30 * months)
        if start < floor:
            raise OverflowError
    except OverflowError:
        most = max(0, (now - floor).days // 30)
        raise ValueError(f"backfill_months: {months} months reaches back past the earliest date this agent can compute (year 1); the most it can read back is {most} months") from None
    return f"{start.year:04d}-{start.month:02d}-{start.day:02d}T{start.hour:02d}:{start.minute:02d}:{start.second:02d}Z"


def repo_entries(raw: Any) -> list[str]:
    """Comma- or newline-separated entries; each is owner/repo or a bare organisation/user name. Semicolons are not separators."""
    return [t.strip() for t in re.split(r"[,\n]", str(raw or "")) if t.strip()]


def validate_inputs(inp: dict) -> list[str]:
    errs: list[str] = []
    bm = inp.get("backfill_months")
    if not _blank(bm) and parse_months(bm) is None:
        errs.append(f"backfill_months: {bm!r} is not a positive whole number of months (zero, negatives, fractions and text are rejected)")
    rq = inp.get("request")
    if not _blank(rq) and str(rq).strip().lower() not in REQUESTS + ("auto",):  # "auto" is the internal alias for the default
        errs.append(f"request: {rq!r} is not one of {', '.join(REQUESTS)}")
    ds = inp.get("docs_source")
    if not _blank(ds) and str(ds).strip().lower() != "all three":
        parts = [p.strip().lower() for p in str(ds).split("+") if p.strip()]
        bad = [p for p in parts if p not in DOCS_PARTS]
        if bad or not parts or ("none" in parts and len(parts) > 1):
            errs.append(f"docs_source: {ds!r} is not one of None, Confluence, Notion, Google Drive, Confluence + Notion, Confluence + Google Drive, Notion + Google Drive, All three")
    for name in ("include_dms", "keep_internal_details"):
        v = inp.get(name)
        if not _blank(v) and str(v).strip().lower() not in {"yes", "no"}:
            errs.append(f"{name}: {v!r} must be Yes or No")
    df, dt = inp.get("period_from"), inp.get("period_to")
    for label, v in (("period_from", df), ("period_to", dt)):
        if not _blank(v) and _iso(str(v)) is None:
            errs.append(f"{label}: {v!r} is not a real date in YYYY-MM-DD form")
    if not _blank(df) and not _blank(dt):
        d1, d2 = _iso(str(df)), _iso(str(dt))
        if d1 and d2 and d1 > d2:
            errs.append(f"period_from ({df}) is after period_to ({dt})")
    for n, line in enumerate(str(inp.get("team_changes") or "").splitlines(), 1):
        if line.strip():
            m = _TEAM_LINE.match(line)
            if not m or _iso(m.group(1)) is None:
                errs.append(f"team_changes line {n}: {line.strip()!r} must look like YYYY-MM-DD: Team name, with a real date")
    if ";" in str(inp.get("repos_or_org") or ""):
        errs.append("repos_or_org: separate entries with commas or new lines, not semicolons")
    else:
        for t in repo_entries(inp.get("repos_or_org")):
            if not (_REPO.match(t) or _OWNER.match(t)):
                errs.append(f"repos_or_org: {t!r} is not owner/repo or a bare organisation or user name")
    for name in ("jira_projects", "slack_channels", "slack_user_ids", "redact_terms"):
        if name != "redact_terms" and ";" in str(inp.get(name) or ""):
            errs.append(f"{name}: separate entries with commas or new lines, not semicolons")
    tz = inp.get("timezone")
    if not _blank(tz):
        from zoneinfo import available_timezones
        if str(tz).strip() not in available_timezones():  # IANA names are case-sensitive
            errs.append(f"timezone: {tz!r} is not an IANA timezone name such as Asia/Kolkata")
    return errs
