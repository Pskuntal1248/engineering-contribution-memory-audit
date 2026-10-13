"""Rules the model cannot be trusted with. Code enforces them after the model speaks."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from models import Item

TRIVIAL_TITLE = re.compile(r"^\s*(initial commit|first commit|init(ial(ize|ise)?)?\b|repository init\w*|repo init\w*|bootstrap|scaffold|add (readme|license|gitignore)|update readme|readme|chore: ?init|update [\w./-]+\.(md|txt)\b|update print statement|fix typo|typo)", re.I)

ROLES = ("led", "built", "contributed", "reviewed", "supported")


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    """Model-call and wall-clock limits from the build spec, enforced here, not hoped for."""

    max_calls: int
    max_seconds: float
    calls: int = 0
    started: float = field(default_factory=time.monotonic)
    by_kind: dict = field(default_factory=dict)  # attempts charged per kind ("embedding"; everything else is chat)

    def spend(self, kind: str = "model", reserve: int = 0) -> None:
        """Check the call and time room and charge one attempt, in one synchronous step (callers must not await between this and the network send).
        `reserve` keeps that many calls for work that is still pending: an optional step may not use them."""
        if self.calls >= self.max_calls - reserve:
            raise BudgetExceeded(f"model-call limit of {self.max_calls} reached" if not reserve else
                                 f"only {max(0, self.max_calls - self.calls)} of {self.max_calls} model calls are left and {reserve} are kept for work still pending")
        if self.elapsed() > self.max_seconds:
            raise BudgetExceeded(f"time limit of {self.max_seconds:.0f}s reached")
        self.calls += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def room(self, reserve: int = 0) -> int:
        return max(0, self.max_calls - self.calls - reserve)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def out_of_time(self, reserve: float = 0.0) -> bool:
        return self.elapsed() > self.max_seconds - reserve


MODE_LIMITS = {  # (max model calls, max seconds), from the spec's Limits line
    "backfill": (150, 30 * 60),
    "weekly": (25, 5 * 60),
    "on_demand": (10, 2 * 60),
}


def budget_for(mode: str) -> Budget:
    calls, secs = MODE_LIMITS["on_demand" if mode not in MODE_LIMITS else mode]
    return Budget(calls, secs * 0.9)  # 10% margin under the stated limit


# ------------------------------------------------------------ tool log / DMs
@dataclass
class ToolLog:
    """Every source read, so 'no DM reads when include_dms is No' can be shown from the log."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(self, tool: str, **args: Any) -> None:
        self.entries.append({"tool": tool, **args})

    def dm_reads(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e.get("is_dm")]


def parse_channels(text: str) -> list[str]:
    return [c.strip().lstrip("#").strip() for c in re.split(r"[,\n]", text or "") if c.strip().lstrip("#").strip()]


def is_dm_channel(ch: dict[str, Any]) -> bool:
    """Slack: is_im / is_mpim, or ids D... (DM) / G... (legacy group DM). Private channels are not DMs but still need opt-in."""
    return bool(ch.get("is_im") or ch.get("is_mpim")) or str(ch.get("id", "")).startswith("D")


def channels_to_read(all_channels: list[dict[str, Any]], chosen: list[str], include_dms: bool,
                     log: ToolLog | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Only channels the user named. DMs only when include_dms. Returns (channels, not_found)."""
    names = {c.lower() for c in chosen}
    picked, found = [], set()
    for ch in all_channels:
        nm = str(ch.get("name", "")).lower()
        if is_dm_channel(ch):
            ids = {nm, str(ch.get("id", "")).lower(), str(ch.get("user", "")).lower()} - {""}
            if include_dms and (ids & names or "*dm*" in names or not names):
                picked.append(ch)
            elif log is not None:
                log.add("skipped_channel", channel=ch.get("id"), reason="dm excluded")
            continue
        ids = {nm, str(ch.get("id", "")).lower()} - {""}  # a channel can be named by its exact name or its ID
        if ids & names:
            picked.append(ch)
            found |= ids & names
    found |= {str(c.get("id", "")).lower() for c in picked} | {str(c.get("user", "")).lower() for c in picked}
    return picked, sorted(names - found - {"*dm*"})


def drop_unconsented(items: Iterable[Item], include_dms: bool) -> tuple[list[Item], int]:
    keep, dropped = [], 0
    for it in items:
        if it.source == "slack" and it.meta.get("is_dm") and not include_dms:
            dropped += 1
            continue
        keep.append(it)
    return keep, dropped


# --------------------------------------------------------------- statements
@dataclass
class Statement:
    text: str
    evidence: list[str]
    role: str = ""

    def __post_init__(self) -> None:
        self.text = first_person(self.text)


def keep_cited(statements: list[Statement], known_ids: set[str]) -> tuple[list[Statement], int]:
    """Every statement links to at least one real item. Unknown ids are removed; no ids left, statement dropped."""
    out, dropped = [], 0
    for s in statements:
        ev = [e for e in dict.fromkeys(s.evidence) if e in known_ids]
        if not ev or not s.text.strip():
            dropped += 1
            continue
        out.append(Statement(s.text.strip(), ev, s.role))
    return out, dropped


def cap_role(claimed: str, evidence: list[Item], username: str) -> str:
    """Never inflate. 'led' needs evidence of driving the work; reviews alone are 'reviewed'."""
    claimed = claimed if claimed in ROLES else "contributed"
    mine = [e for e in evidence if e.meta.get("mine")]
    authored = [e for e in mine if e.type in {"pr", "commit", "doc"} and (e.author or "").lower() == username.lower()]
    only_reviews = bool(mine) and all(e.type == "review" for e in mine)
    if not mine:
        return "supported" if claimed == "supported" else "reviewed" if any(e.type == "review" for e in evidence) else "supported"
    if only_reviews:
        return "reviewed"
    if claimed in {"led"}:
        prs = [e for e in evidence if e.type == "pr"]
        my_prs = [e for e in prs if e.meta.get("mine")]
        drove = (
            any(e.type == "doc" and (e.author or "").lower() == username.lower() for e in evidence)
            or any(e.type == "ticket" and e.meta.get("reporter", "").lower() == username.lower() for e in evidence)
            or (prs and len(my_prs) * 2 > len(prs))
        )
        if not drove:
            return "contributed"
    if claimed == "built" and not authored:
        return "contributed"
    return claimed


def co_contributors(evidence: list[Item], username: str) -> list[str]:
    names: dict[str, None] = {}
    for e in evidence:
        for n in [e.author, *e.co_authors, *(e.meta.get("participants") or [])]:
            if n and n.lower() != username.lower() and not n.startswith("U0"):
                names[n] = None
    return sorted(names)


# Assessing a colleague is forbidden. These patterns catch a named person plus a judgement of them.
_JUDGE = r"(slow|sloppy|careless|incompetent|weak|poor|lazy|unreliable|difficult|junior|underperform\w*|struggl\w*|blocked us|dropped the ball|not (good|strong|reliable)|needs? (to improve|coaching)|bad at|better than|worse than)"


def judges_colleague(text: str, colleagues: Iterable[str]) -> bool:
    for name in colleagues:
        n = re.escape(name.split()[0]) if name else ""
        if not n:
            continue
        if re.search(rf"\b{n}\b[^.\n]{{0,60}}\b{_JUDGE}\b", text, re.I) or re.search(rf"\b{_JUDGE}\b[^.\n]{{0,40}}\b{n}\b", text, re.I):
            return True
    return False


def drop_colleague_assessments(statements: list[Statement], colleagues: Iterable[str]) -> tuple[list[Statement], int]:
    cols = list(colleagues)
    keep = [s for s in statements if not judges_colleague(s.text, cols)]
    return keep, len(statements) - len(keep)


# ------------------------------------------------------------ evidence gaps
_PROMO = re.compile(r"\b(will|would|should|likely|on track|ready|deserve\w*|bound)\b[^.\n]{0,30}\b(promot\w+|be promoted|get promoted)\b|\bpromot\w+\b[^.\n]{0,25}\b(likely|unlikely|certain|guaranteed|is not going|will not|won't)\b", re.I)


def predicts_promotion(text: str) -> bool:
    return bool(_PROMO.search(text))


# -------------------------------------------------------------- confidential
FLAG = "[CONFIDENTIAL source - check before sharing]"


def confidential_ids(items: Iterable[Item]) -> set[str]:
    return {i.id for i in items if i.meta.get("confidential")}


def flag_confidential(st: Statement, conf: set[str]) -> Statement:
    if any(e in conf for e in st.evidence) and FLAG not in st.text:
        return Statement(f"{FLAG} {st.text}", st.evidence, st.role)
    return st


# ------------------------------------------------------------------ scrubbing
_MONEY = re.compile(r"[$€£₹]\s?\d[\d,\.]*\s?(?:[kKmMbB]|lakh|crore)?\b|\b\d[\d,]{3,}\b|\b\d+(?:\.\d+)?\s?(?:k|M|B|%|x)\b")


def _term_re(t: str) -> "re.Pattern[str]":
    """The term as a whole word, case-insensitive, with its simple plural and possessive forms: Acme, Acmes, Acme's, Acmes', Acme\u2019s;
    a term ending in y also matches -ies (company, companies). Anything longer or different is a different word and is left alone."""
    forms = re.escape(t) + r"(?:['\u2019]s|s['\u2019]?|es)?"
    if len(t) > 2 and t.lower().endswith("y"):
        forms += "|" + re.escape(t[:-1]) + r"ies(?:['\u2019])?"
    return re.compile(rf"(?<![\w-])(?:{forms})(?![\w-])", re.I)


def has_term(text: str, terms: Iterable[str]) -> bool:
    return any(_term_re(t.strip()).search(text or "") or t.strip().lower() in (text or "").lower() for t in terms if t and len(t.strip()) > 1)


def scrub(text: str, terms: Iterable[str], keep_internal: bool = False, strip_figures: bool = True) -> str:
    """Interview stories: remove internal names, customers and figures unless the user keeps them."""
    if keep_internal:
        return text
    out = text
    for t in sorted({t.strip() for t in terms if t and len(t.strip()) > 1}, key=len, reverse=True):
        out = _term_re(t).sub("[internal]", out)
    if strip_figures:
        out = _MONEY.sub("[figure]", out)
    return out


def internal_terms(items: Iterable[Item], extra: Iterable[str], org: str, site: str) -> set[str]:
    """Names we must strip: configured customers/people, org, Jira site, repo names, and 'Customer: X' mentions."""
    terms = {t.strip() for t in extra if t.strip()}
    if org:
        terms.add(org)
    m = re.search(r"https?://([a-z0-9-]+)\.", site or "", re.I)
    if m:
        terms.add(m.group(1))
    for it in items:
        if it.meta.get("repo"):
            terms.add(it.meta["repo"].split("/")[-1])
            terms.add(it.meta["repo"].split("/")[0])
        for mm in re.finditer(r"\b[Cc]ustomer:?\s+([A-Z][\w&-]+(?:\s[A-Z][\w&-]+)?)", it.text or ""):
            terms.add(mm.group(1))
        for mm in re.finditer(r"\b([A-Z][\w&-]+(?:\s[A-Z][\w&-]+)?)\s+\((?:customer|client|merchant)\)", it.text or ""):
            terms.add(mm.group(1))
    return {t for t in terms if t}


def clip(text: str, n: int = 220) -> str:
    """Shorten at a sentence end if one fits, else at a word, with an ellipsis. Never cuts mid-word."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= n:
        return t
    head = t[:n]
    k = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if k >= n * 0.2:
        return head[: k + 1]
    cut = (head.rsplit(" ", 1)[0] if t[n] != " " else head).rstrip(" ,;:-(")
    while " " in cut and cut.rsplit(" ", 1)[1].lower() in {"a", "an", "the", "of", "to", "and", "or", "for", "with", "in", "on", "by", "at", "from", "how", "is", "are", "i", "we"}:
        cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:-(")
    return cut + "..."


_FP = [(r"\bthe user's\b", "my"), (r"\bthe user is\b", "I am"), (r"\bthe user has\b", "I have"), (r"\bthe user was\b", "I was"),
       (r"\bthe user\b", "I"), (r"\bUser's\b", "My"), (r"\bthe engineer\b", "I")]


def first_person(text: str) -> str:
    """Model text sometimes says 'the user ...'; statements are written as 'I ...' like the 1:1."""
    out = text or ""
    for rx, rep in _FP:
        out = re.sub(rx, rep, out, flags=re.I if rx.startswith(r"\bthe") else 0)
    out = re.sub(r"\bThe (?:(?:accepted|proposed|approved|final) )?(?:text|doc|document|page|ADR|design)\s+(?:decides|decided)\s+(?:to|that)\s+", "We will ", out)
    out = re.sub(r"^Proposes:\s*", "We propose to ", out)
    out = re.sub(r"\bThe (?:text|doc|document|page)\s+(?:states|says|records|proposes)\s+(?:that\s+)?", "", out)
    return re.sub(r"(^|[.!?] )i ", lambda m: m.group(1) + "I ", out)


_IMPERATIVE = re.compile(r"^(?:Use|Run|Move|Adopt|Build|Add|Replace|Switch|Keep|Store|Migrate|Implement|Split|Introduce|Choose|Create|Remove|Drop|Make|Deploy|Route|Send|Write|Cache|Batch|Queue)\b")


def decision_voice(text: str) -> str:
    """One voice for every decision: 'We will ...' (a decision statement), whatever form the model or the rule produced."""
    out = first_person(text or "").strip()
    if _IMPERATIVE.match(out):
        out = "We will " + out[0].lower() + out[1:]
    return out


def err_text(e: BaseException, n: int = 160) -> str:
    """The real message of an error, looking inside ExceptionGroups ("unhandled errors in a TaskGroup")."""
    subs = getattr(e, "exceptions", None)
    if subs:
        return "; ".join(dict.fromkeys(err_text(x, n) for x in subs))[:n]
    return (str(e) or e.__class__.__name__)[:n]

