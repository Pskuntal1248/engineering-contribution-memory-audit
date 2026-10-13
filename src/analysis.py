"""Judgement steps. Each has a deterministic baseline (no model) and an optional model pass.
The model returns typed objects with evidence ids; code drops anything not backed by a real
item, caps roles, removes colleague assessments and counts every number."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from guards import Budget, BudgetExceeded, Statement, cap_role, clip, co_contributors, decision_voice, first_person, drop_colleague_assessments, keep_cited
from linking import tokens
from metrics import shipped
from models import Item, day

# ----------------------------------------------------------------- schemas (typed model outputs)
Label = Literal["problem_solved", "decision", "help_given", "mentoring", "none"]


_CLAIMS = re.compile(r"\b(redone|redo|rework(?:ed)?|rewritten|rolled back|abandoned|cancell?ed|postponed|failed|delayed)\b", re.I)


def _supported(text: str, evidence_text: str) -> str:
    """A model sentence about the project must share at least 2 content words with its linked evidence, else it is dropped."""
    words = {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}
    return text if len(words & set(re.findall(r"[a-z]{4,}", evidence_text))) >= 2 else ""


_STOP = {"that", "this", "with", "from", "which", "have", "were", "been", "into", "work", "built", "support", "supported", "team", "project", "shipped"}


def _grounded(text: str, evidence_text: str) -> str:
    """Drop a clause that claims redo/rollback/failure/delay when no source text uses that word."""
    parts = re.split(r"(?<=[.;])\s+", text or "")
    keep = [p for p in parts if not any(m.group(1).lower() not in evidence_text for m in _CLAIMS.finditer(p))]
    return " ".join(keep).strip()


class ThreadVerdict(BaseModel):
    thread_id: str = Field(description="the id given in the input, copied exactly")
    label: Label
    summary: str = Field(description="one sentence from the thread text only: what was wrong and what the user did")
    user_role: Literal["led", "built", "contributed", "reviewed", "supported"] = "supported"


class ThreadVerdicts(BaseModel):
    verdicts: list[ThreadVerdict]


class ProjectCard(BaseModel):
    project_id: str
    name: str
    goal: str = Field(description="from the evidence only")
    outcome: str = Field(description="from the evidence only; say 'not stated' if unknown")
    role: Literal["led", "built", "contributed", "reviewed", "supported"]
    evidence: list[str] = Field(description="item ids copied exactly from the input")


class ProjectCards(BaseModel):
    projects: list[ProjectCard]


class LLM(Protocol):
    async def structured(self, schema: type[BaseModel], system: str, payload: dict[str, Any], budget: Budget | None = None) -> BaseModel: ...


# ---------------------------------------------------------------------------- baselines
_PROBLEM = re.compile(r"\b(traced|root cause|fixed|fix(?:ed)?|patched|cleared|pinned|moved it to|found (?:the|that)|took the page|debug\w*|workaround|added a lock)\b", re.I)
_HELP = re.compile(r"\b(walked through|explained|shared the|showed|here is the|use the|advised|paired|how (?:it )?works?|replay)\b", re.I)
_MENTOR = re.compile(r"\b(mentored|paired|walked (?:him|her|them) through|new joiner|onboard\w*|taught|showed the token)\b", re.I)
_DECISION = re.compile(r"\b(we decided|decision|chose|chosen|going with|agreed to|approach:)\b", re.I)


def has_strong_link(it: Item, links: dict[str, list[str]]) -> bool:
    return bool(it.keys) or any(not (r.startswith("close in time") or r.startswith("same topic")) for r in links.get(it.id, []))


def my_text(it: Item, display_names: set[str]) -> str:
    lines = [l.split(": ", 1) for l in it.text.splitlines() if ": " in l]
    mine = [b for a, b in lines if a.strip() in display_names]
    return " ".join(mine) or it.text


def _counts_line(prs: int, tickets: int) -> str:
    parts = [f"{n} {w}{'' if n == 1 else 's'} {v}" for n, w, v in ((prs, "PR", "shipped"), (tickets, "ticket", "done")) if n]
    return (" and ".join(parts) + ".") if parts else ""


def baseline_thread(it: Item, display_names: set[str]) -> tuple[Label, str]:
    t = my_text(it, display_names)
    if _DECISION.search(t):
        label: Label = "decision"
    elif _MENTOR.search(t):
        label = "mentoring"
    elif _PROBLEM.search(t):
        label = "problem_solved"
    elif _HELP.search(t):
        label = "help_given"
    else:
        label = "none"
    first = re.sub(r"\s+", " ", t).strip()
    return label, clip(first, 220)


def find_invisible(items: list[Item], links: dict[str, list[str]], display_names: set[str],
                   verdicts: dict[str, ThreadVerdict] | None = None) -> list[dict[str, Any]]:
    """Slack threads the user took part in with no ticket, PR or doc link: the work nobody recorded."""
    out = []
    for it in items:
        if it.type != "thread" or not it.meta.get("mine") or has_strong_link(it, links):
            continue
        v = (verdicts or {}).get(it.id)
        label, summary = (v.label, first_person(clip(v.summary, 220))) if v else baseline_thread(it, display_names)
        if label == "none":
            continue
        # a thread where the user only posted a short ack is not work
        if it.meta.get("my_messages", 0) < 1 or len(my_text(it, display_names).split()) < 9:
            continue
        out.append({"thread": it.id, "label": label, "summary": summary, "evidence": [it.id], "date": it.date,
                    "co_contributors": [p for p in it.meta.get("participants", []) if p not in display_names]})
    return sorted(out, key=lambda d: (d["date"], d["thread"]))


_DECISION_WORDS = re.compile(r"\b(adr|rfc|design docs?|design proposal|proposal|decision records?|decision)\b", re.I)
NEEDS_DECISION = re.compile(r"\b(needs?|needing|awaiting|pending|requires?)\s+(a\s+|an\s+|the\s+)?(decision|approval|sign[- ]?off|call)\b|\bto be decided\b|\bopen question\b", re.I)
_DECISION_PATH = re.compile(r"(^|/)(adrs?|rfcs?|decisions?)/|(^|/)docs/design/", re.I)
_STRONG_WORDS = re.compile(r"\b(adr(-?\d+)?|rfc(-?\d+)?|design docs?|architecture decision records?)\b", re.I)


def is_decision_candidate(it: Item) -> bool:
    """A PR or doc that looks like an ADR / design doc / proposal: by title, labels, branch or the path it adds."""
    if it.type not in {"pr", "doc"} or not it.meta.get("mine") or it.meta.get("is_revert") or it.meta.get("reverted"):
        return False
    hay = " ".join([it.title, " ".join(it.meta.get("labels", []) or []), str(it.meta.get("branch", ""))])
    if it.type == "doc" and re.search(r"\b(design|architecture)\b", it.title, re.I):
        return True
    return bool(_DECISION_WORDS.search(hay) or any(_DECISION_PATH.search(p) for p in it.meta.get("paths", []) or []))


def is_strong_decision(it: Item) -> bool:
    """ADR / RFC / design-doc by name or path: counted by rule, the model can only word it, never veto it."""
    if not is_decision_candidate(it):
        return False
    hay = " ".join([it.title, " ".join(it.meta.get("labels", []) or []), str(it.meta.get("branch", ""))])
    if it.type == "doc" and re.search(r"\b(design|architecture)\b", it.title, re.I):
        return True  # a design page the user wrote records a design choice; this must not depend on the stored text length
    return bool(_STRONG_WORDS.search(hay) or any(_DECISION_PATH.search(p) for p in it.meta.get("paths", []) or []))


class DecisionVerdict(BaseModel):
    item_id: str = Field(description="the id given in the input, copied exactly")
    is_decision: bool = Field(description="true only if the text records a technical decision or design choice")
    summary: str = Field(description="one sentence from the given text only: what was decided")


class DecisionVerdicts(BaseModel):
    verdicts: list[DecisionVerdict]


async def classify_decisions_with_model(llm: LLM | None, budget: Budget, cands: list[Item], batch: int = 10,
                                        max_batches: int = 5) -> tuple[dict[str, DecisionVerdict], list[str]]:
    """Model pass on ADR / design-doc candidates: confirms they record a decision and writes the one-line summary."""
    out: dict[str, DecisionVerdict] = {}
    if not cands:
        return out, []
    if llm is None:
        return out, ["model not used for decisions (rules only)"]
    notes: list[str] = []
    sys = ("You check whether each item records a technical decision or design choice made by the user (an ADR, design doc, RFC or proposal). "
           "Use ONLY the given text. Say what was decided in one sentence. Copy item_id exactly.")
    sent = cands[: batch * max_batches]
    for i in range(0, len(sent), batch):
        chunk = sent[i : i + batch]
        payload = {"items": [{"item_id": c.id, "title": c.title, "text": c.text[:800], "files": (c.meta.get("paths") or [])[:6]} for c in chunk]}
        try:
            res = await llm.structured(DecisionVerdicts, sys, payload, budget)
        except BudgetExceeded as e:
            notes.append(str(e))
            break
        except Exception as e:  # noqa: BLE001
            notes.append(f"model call failed for decisions: {str(e)[:120]}")
            continue
        ok = {c.id for c in chunk}
        for v in res.verdicts:  # type: ignore[attr-defined]
            if v.item_id in ok:
                out[v.item_id] = v
    return out, notes


_HEADS = r"Context|Background|Summary|Overview|Problem|Goals?|Decision|Outcome|Resolution|Proposal|Status|Owner|Date|Authors?|Reviewers?|Deciders?|Consequences|Options"


def decision_owner(text: str) -> str:
    m = re.search(r"\bOwner\s*:\s*((?:(?!(?:" + _HEADS + r")\b)[A-Z][\w.'-]*\s*){1,3})", text or "")
    return m.group(1).strip() if m else ""


def decision_sentence(text: str) -> str:
    """What was decided, as one full sentence: the text under a Decision/Outcome/Proposal heading, else the first sentence after the page header lines (Owner, Status, Context heading)."""
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.search(r"\b(?:Decision|Outcome|Resolution|Proposal)\s*:?\s+([A-Z].+)", t)
    if m:
        t = m.group(1)
    else:
        t = re.sub(r"\b(?:Owner|Status|Date|Authors?|Reviewers?|Deciders?)\s*:\s*(?:(?!(?:" + _HEADS + r")\b)[A-Za-z0-9][\w.'-]*\s*){0,4}", "", t)
        t = re.sub(r"^\s*(?:(?:" + _HEADS + r")\b\s*:?\s*)+", "", t).strip()
    s = re.match(r"(.+?[.!?])(?:\s|$)", t)
    return clip((s.group(1) if s else t).strip(), 220)


def find_decisions(items: list[Item], display_names: set[str], username: str,
                   verdicts: dict[str, DecisionVerdict] | None = None) -> list[dict[str, Any]]:
    """Design docs / ADRs, as docs or as PRs. Role: author = led; editor or reviewer only = contributed.
    ADR/RFC/design-doc items (by name or path) always count; the model can only add a summary. Weaker candidates
    (words like proposal or decision) need a model "yes", or the decision wording in a doc's own text when there is no model."""
    out = []
    for it in items:
        if not is_decision_candidate(it):
            continue
        v = (verdicts or {}).get(it.id)
        if not is_strong_decision(it):
            ok = v.is_decision if v is not None else (it.type == "pr" or bool(_DECISION.search(it.text)) or bool(it.meta.get("decision_text")))
            if not ok:
                continue
        owner = str(it.meta.get("owner") or decision_owner(it.text))
        author_is_me = it.author in display_names or it.author.lower() == username.lower() or bool(owner and (owner in display_names or owner.lower() == username.lower() or owner.lower() in {n.lower() for n in display_names}))
        summary = decision_voice(clip(v.summary, 220) if v is not None and v.is_decision and v.summary else (str(it.meta.get("dsum") or "") or decision_sentence(it.text) or clip(it.title, 220)))
        out.append({"doc": it.id, "title": it.title, "role": "led" if author_is_me else "contributed", "evidence": [it.id],
                    "summary": summary, "confidential": bool(it.meta.get("confidential")),
                    "co_contributors": [] if author_is_me else [it.author]})
    return sorted(out, key=lambda d: d["doc"])


_NAME_STOP = set("a an the and or to of in on for with from by at as is it this that add adds added fix fixes fixed update updates updated remove use make new into via initial commit pr merge revert docs doc design adr rfc wip chore feat feature bug task story epic ticket work follow followup".split())


def project_name(ev: list[Item]) -> str:
    """Readable name: the words that recur in the titles (full words, original spelling), plus the Jira project key.
    Falls back to the earliest work title with its key and leading verb removed."""
    work = [e for e in ev if e.type in {"ticket", "pr", "doc"} and not e.title.lower().startswith("revert")]
    prefixes = Counter(k.split("-")[0] for e in ev if e.type not in {"thread", "doc"} for k in e.keys if "-" in k)
    key = prefixes.most_common(1)[0][0] if prefixes else ""

    def words(title: str) -> list[str]:
        t = re.sub(r"\b[A-Z][A-Z0-9]+-\d+\b[:\s]*", "", title)
        return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", t) if w.lower() not in _NAME_STOP]
    norm = lambda w: w.lower().rstrip("s")
    count: Counter[str] = Counter()
    first: dict[str, str] = {}
    for e in sorted(work, key=lambda x: x.date):
        for w in set(norm(w) for w in words(e.title)):
            count[w] += 1
        for w in words(e.title):
            first.setdefault(norm(w), w.lower())
    ranked = sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))
    src = sorted(work, key=lambda x: x.date)
    if ranked and ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] >= 2 * ranked[1][1]):
        label = first[ranked[0][0]]  # one word clearly dominates the titles: "payouts"
    else:  # otherwise the earliest work title, in its own word order, without the key and the leading verb
        label = " ".join(words(src[0].title)[:4]).lower() if src else ""
    label = label[:1].upper() + label[1:]
    return f"{label} ({key})" if label and key else label or key


def baseline_project_card(pid: str, ids: list[str], by_id: dict[str, Item], username: str) -> dict[str, Any]:
    ev = [by_id[i] for i in ids]
    tickets = [e for e in ev if e.type == "ticket"]
    name = project_name(ev)
    prs = [e for e in ev if e.type == "pr" and e.meta.get("mine")]
    shipped_n = sum(1 for e in prs if shipped(e))
    role = cap_role("built" if prs else "contributed", ev, username)
    dates = sorted(day(e) for e in ev)
    return {"project_id": pid, "name": name or pid, "goal": "not stated in the sources (model summary needed)",
            "outcome": _counts_line(shipped_n, sum(1 for t in tickets if t.meta.get("status", "").lower() in {"done", "closed", "resolved"})),
            "role": role, "first": dates[0], "last": dates[-1], "evidence": sorted(ids, key=lambda i: (by_id[i].date, i))[:60]}


SKILL_LEXICON = {
    "payments & idempotency": r"idempot|payout|upi|ledger|settlement",
    "async messaging (queues)": r"queue|webhook|consumer|dead letter",
    "frontend & accessibility": r"accessib|contrast|design system|storybook|checkout (page|flow|form)|a11y|css",
    "debugging & incident response": r"incident|pager|root cause|traced|debugg|stack trace|postmortem",
    "code review": r"code review|reviewed (the |his |her |their )?(pr|pull request)|review comments?|lgtm|nit:|requested changes",
    "mentoring": r"mentor|paired|new joiner|onboard",
    "technical design": r"adr|design doc|design:|rfc|decision",
}


_RANK = {"pr": 0, "ticket": 1, "doc": 2, "thread": 3, "commit": 4, "review": 5}


def map_skills(items: list[Item]) -> list[dict[str, Any]]:
    out = []
    for skill, pat in sorted(SKILL_LEXICON.items()):
        rx = re.compile(pat, re.I)
        ids = []
        for i in items:
            if not (i.meta.get("mine") or i.type == "review"):
                continue
            if skill == "code review" and i.type not in {"review", "thread"}:
                continue  # only review comments or a conversation about a review show code review; a doc, ticket or PR does not
            hits = {m.group(0).lower() for m in rx.finditer(i.title + " " + i.text[:600])}
            in_title = bool(rx.search(i.title))
            # a passing mention in a body is not evidence: PRs, tickets and commits must name the skill in their title; docs in the title or with two terms; threads on any hit
            if i.type in {"pr", "ticket", "commit"}:
                ok_ = in_title
            elif i.type == "doc":
                ok_ = in_title or len(hits) >= 2
            else:
                ok_ = bool(hits) if i.type in {"thread", "review"} else (in_title or len(hits) >= 2)
            if ok_:
                ids.append(i.id)
        if len(ids) >= 2 or (len(ids) == 1 and (re.search(pat, next(i.title for i in items if i.id == ids[0]), re.I) or next(i.type for i in items if i.id == ids[0]) == "thread")):
            by = {i.id: i for i in items}
            work = _distinct_work([by[x] for x in ids])  # a PR, its ticket and its commits are one piece of work
            rank = {"doc": 0, "pr": 1, "ticket": 2, "thread": 3, "commit": 4, "review": 5} if skill == "technical design" else _RANK  # design: the pages the user wrote first
            out.append({"skill": skill, "count": len(work), "evidence": sorted(ids, key=lambda x: (rank.get(by[x].type, 9), by[x].meta.get("docs_only", False), x))[:6]})
    return sorted(out, key=lambda d: (-d["count"], d["skill"]))


def _distinct_work(its: list[Item]) -> set[str]:
    """Distinct pieces of work: items that share any ticket key (a PR, its ticket, commits that name an old key) are one; keyless items go by title."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in its:
        ks = list(i.keys) or ["title:" + re.sub(r"^[A-Z][A-Z0-9]+-\d+[:\s-]*", "", i.title).strip().lower()]
        m = re.match(r"^([A-Z][A-Z0-9]+-\d+)\b", i.title)
        if m:
            ks.append(m.group(1))
        for k in ks[1:]:
            parent[find(k)] = find(ks[0])
        find(ks[0])
    return {find(k) for k in parent}


# --------------------------------------------------------------------------- model passes
async def classify_threads_with_model(llm: LLM | None, budget: Budget, threads: list[Item], names: set[str],
                                      batch: int = 15, max_batches: int = 40) -> tuple[dict[str, ThreadVerdict], list[str]]:
    """One call per `batch` candidate threads. Returns verdicts keyed by id, plus notes on anything skipped.
    Only ids we sent are accepted back."""
    notes: list[str] = []
    verdicts: dict[str, ThreadVerdict] = {}
    if llm is None:
        return verdicts, ["model not used for thread labels (baseline rules)"]
    sys = ("You label Slack threads for an engineer's work record. For each thread say whether the user solved a problem, "
           "took part in a decision, gave help or mentored, or none. Use ONLY the thread text. Never judge other people. "
           "Write each summary in the first person as the engineer ('I traced...'), never 'the user'. Copy thread_id exactly.")
    sent = threads[: batch * max_batches]
    if len(threads) > len(sent):
        notes.append(f"{len(threads) - len(sent)} threads not sent to the model (call cap); labelled by rules")
    for i in range(0, len(sent), batch):
        chunk = sent[i : i + batch]
        payload = {"user_names": sorted(names), "threads": [{"thread_id": t.id, "text": t.text[:700]} for t in chunk]}
        try:
            res = await llm.structured(ThreadVerdicts, sys, payload, budget)
        except BudgetExceeded as e:
            notes.append(str(e))
            break
        except Exception as e:  # noqa: BLE001
            notes.append(f"model call failed: {str(e)[:120]}")
            continue
        ok = {t.id for t in chunk}
        for v in res.verdicts:  # type: ignore[attr-defined]
            if v.thread_id in ok:
                verdicts[v.thread_id] = v
    return verdicts, notes


async def project_cards_with_model(llm: LLM | None, budget: Budget, base: list[dict[str, Any]], by_id: dict[str, Item],
                                   username: str, per_call: int = 5) -> tuple[list[dict[str, Any]], list[str]]:
    """Names, goals, outcomes and roles. Evidence ids are validated; role is capped by code."""
    notes: list[str] = []
    if llm is None:
        return base, ["project names and goals from rules (model not used)"]
    sys = ("Name each project and state its goal and outcome using ONLY the evidence given. Say 'not stated' when unknown. "
           "Role: led only if the user drove the work; reviewing is 'reviewed'. Write goal and outcome in the first person ('I built...'), complete sentences under 200 characters, never 'the user'. Copy ids exactly.")
    out = {b["project_id"]: dict(b) for b in base}
    for i in range(0, len(base), per_call):
        chunk = base[i : i + per_call]
        payload = {"projects": [{"project_id": b["project_id"], "evidence": [{"id": e, "title": by_id[e].title, "text": by_id[e].text[:220], "type": by_id[e].type} for e in b["evidence"][:25]]} for b in chunk]}
        try:
            res = await llm.structured(ProjectCards, sys, payload, budget)
        except BudgetExceeded as e:
            notes.append(str(e)); break
        except Exception as e:  # noqa: BLE001
            notes.append(f"model call failed: {str(e)[:120]}"); continue
        for c in res.projects:  # type: ignore[attr-defined]
            if c.project_id not in out:
                continue
            ev = [e for e in c.evidence if e in by_id] or out[c.project_id]["evidence"]
            src_text = " ".join(by_id[e].text + " " + by_id[e].title for e in ev).lower()
            out[c.project_id].update(name=clip(c.name, 80), goal=_supported(_grounded(first_person(clip(c.goal, 240)), src_text), src_text) or out[c.project_id]["goal"], outcome=_supported(_grounded(first_person(clip(c.outcome, 240)), src_text), src_text) or out[c.project_id]["outcome"], role=cap_role(c.role, [by_id[e] for e in ev], username))
    return list(out.values()), notes
