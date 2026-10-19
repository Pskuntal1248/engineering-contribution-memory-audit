"""Corpus + result builders. Every number comes from the profile (computed in code); prose is templated
from facts, optionally reworded by the model with evidence ids that code validates."""
from __future__ import annotations

import re
from typing import Any

import ledger
from analysis import NEEDS_DECISION
from guards import FLAG, clip, has_term, TRIVIAL_TITLE, Statement, drop_colleague_assessments, flag_confidential, internal_terms, keep_cited, predicts_promotion, scrub
from metrics import shipped
from models import Item, day

LEDGER_CAP = 6000
ROW_CAP_IN_RESULT = 100  # the result object carries the newest rows; the CSV download carries all of them


_SLACK_ID = re.compile(r"\b[UW](?=[A-Z0-9]*\d)[A-Z0-9]{8,11}\b")
_TRIVIAL = TRIVIAL_TITLE
_PLACEHOLDER = re.compile(r"\bnot stated\b|model summary needed|\(unlinked\)", re.I)


_PROBLEM_WORDS = re.compile(r'\b(fix(?:es|ed)?|bug|race|hotfix|mismatch(?:ing|es)?|regression|duplicat(?:e|ed|es)|dropped|failed|timed[- ]out|did not match|could not|locked out)\b', re.I)
_UNRESOLVED = re.compile(r"\b(unresolved|not (?:yet )?(?:fixed|resolved|addressed)|remains|still open|future work|known (?:bug|issue)|won'?t fix|to be fixed)\b", re.I)


def problem_evidence(i: Item) -> bool:
    """A problem the item RESOLVED: problem language in a sentence that does not itself say the problem is still open."""
    text = i.title + ' ' + i.text
    return any(_PROBLEM_WORDS.search(s) and not _UNRESOLVED.search(s) for s in re.split(r'(?<=[.!?])\s+', text))


def problem_summary(i: Item) -> str:
    sentences = re.split(r'(?<=[.!?])\s+', i.text.strip())
    problem = next((s for s in sentences if problem_evidence(Item(id='p', source=i.source, type=i.type, date=i.date, title='', text=s))), '')
    problem = re.sub(r'^\s*[A-Z][A-Z0-9]+-\d+\s*:\s*', '', problem)
    action = next((s for s in sentences if re.match(r'\s*(Adds?|Fix(?:es|ed)?|Introduc(?:es|ed)|Implement(?:s|ed)|Restore(?:s|d))\b', s, re.I) and s != problem), '')
    body = ('Source describes: ' + problem + (' ' + action if action else '')) if problem else 'I fixed ' + i.title + '.'
    return clip(body, 420) + f' Shipped on {day(i)}.'


_DANGLING = {"a", "an", "the", "of", "to", "and", "or", "for", "with", "in", "on", "by", "at", "from", "how", "is", "are", "i", "we"}


def short(title: str, n: int = 70) -> str:
    """A title for display: trailing [tags] dropped, cut at a word boundary, never mid-bracket."""
    t = re.sub(r"\s*\[[^\]]*\]\s*$", "", (title or "").strip()) or (title or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    if cut.count("[") > cut.count("]"):
        cut = cut[: cut.rfind("[")].rstrip()
    cut = cut.rstrip(" ,;:-")
    while " " in cut and cut.rsplit(" ", 1)[1].lower() in _DANGLING:  # never end on "to", "the", "of"...
        cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "..."


def no_placeholders(text: str) -> str:
    """Drop clauses where the model or rules said a thing is not stated; fix the stray punctuation that leaves."""
    t = re.sub(r"[;,]?[ \t]*[^.;\n]*\b(?:not stated|model summary needed)\b[^.;\n]*", "", text, flags=re.I)
    t = re.sub(r"(?<!\.)\.\.(?!\.)", ".", t)  # a doubled full stop becomes one; an ellipsis (...) stays
    return re.sub(r"[ \t]+([.;,])", r"\1", t).strip()


def no_ids(text: str) -> str:
    """Raw Slack user IDs are never shown."""
    t = re.sub(r"\b(with|and|by|from)\s+" + _SLACK_ID.pattern + r"(?:,\s*" + _SLACK_ID.pattern + r")*", "", text)
    t = _SLACK_ID.sub("a teammate", t)
    return re.sub(r"(?<=\S)[ \t]{2,}", " ", t).replace(" .", ".").replace(" ;", ";")


def _clean_obj(x: Any) -> Any:
    if isinstance(x, str):
        return no_placeholders(no_ids(x))
    if isinstance(x, list):
        return [_clean_obj(v) for v in x]
    if isinstance(x, dict):
        return {k: (v if k in {"url", "id", "table"} else _clean_obj(v)) for k, v in x.items()}
    return x


def _link(it: Item | None) -> str:
    return it.url if it and it.url else (it.id if it else "")


_PRI = {"pr": 0, "ticket": 1, "doc": 2, "commit": 3, "review": 4, "thread": 5}


def _cite(ids: list[str], by_id: dict[str, Item], n: int = 3) -> str:
    out = []
    ids = sorted((i for i in ids if i in by_id), key=lambda i: (_PRI.get(by_id[i].type, 9), by_id[i].date, i))
    for i in ids[:n]:
        it = by_id.get(i)
        if it:
            out.append(f"{short(it.title, 60)} ({it.url or it.id})")
    return "; ".join(out)


def _lab(it: Item) -> str:
    return short(it.title, 90 if it.type == "thread" else 60).replace("[", "(").replace("]", ")") or it.id


def link_line(it: Item | None) -> str:
    if not it:
        return ""
    line = f"{_lab(it)} - {it.url}" if it.url else _lab(it)  # the page shows body text as plain text (guide 5.3): a bare URL stays readable and can be opened
    return f"{line} {FLAG}" if it.meta.get("confidential") else line


def _title_key(i: Item) -> str:
    m = re.match(r"^([A-Z][A-Z0-9]+-\d+)\b", i.title)
    return m.group(1) if m else ""


def _group_links(items: list[Item]) -> list[str]:
    """One line per piece of work: a PR and its Jira ticket share a line with both links. A PR is paired with the ticket whose key it
    names (its title first, then its other keys); only the PR or ticket title is shown, never an old key from a commit."""
    tickets = {_title_key(i) or (i.keys[0] if i.keys else ""): i for i in items if i.type == "ticket"}
    groups: dict[str, list[Item]] = {}
    for i in sorted(items, key=lambda x: (x.type != "pr", x.date, x.id)):
        if i.type == "pr":
            tk = _title_key(i)
            named = f"{i.title} {i.meta.get('branch', '')} " + " ".join(re.findall(r"(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?|implement(?:s|ed)?)\s+([A-Z][A-Z0-9]+-\d+)", i.text or "", re.I))
            cand = [tk] if tk and tk in tickets else [k for k in i.keys if k in tickets and k in named.upper()]  # "Related PAY-1" in a body is a mention, not the ticket this PR delivers
            gk = ("key:" + cand[0]) if cand else i.id
        elif i.type == "ticket":
            k = _title_key(i) or (i.keys[0] if i.keys else "")
            gk = ("key:" + k) if k else i.id
        else:
            gk = i.id
        groups.setdefault(gk, []).append(i)
    names = {"pr": "PR", "ticket": "ticket", "commit": "commit", "doc": "doc", "thread": "Slack thread", "review": "review"}
    out = []
    for g in groups.values():
        if len(g) == 1:
            out.append(link_line(g[0]))
            continue
        lab = []
        for i in g:
            nm = names.get(i.type, i.type)
            if i.type == "pr" and i.meta.get("number"):
                nm = f"PR #{i.meta['number']}"
            lab.append(f"{nm} - {i.url}" if i.url else nm)
        out.append(f"{_lab(g[0])}: " + ", ".join(lab))
    return out


def _same_title(title: str, it: Item) -> bool:
    a = re.sub(r"[\s.\u2026]+$", "", re.sub(r"\.\.\.$", "", short(title, 90))).lower()
    b = re.sub(r"[\s.\u2026]+$", "", re.sub(r"\.\.\.$", "", short(it.title, 90))).lower()
    return bool(a) and (a == b or a.startswith(b) or b.startswith(a))


def block(title: str, sentence: str = "", facts: list[str] | None = None, items: list[Item] | None = None, label: str = "Evidence") -> str:
    """One readable item, as separate paragraphs (markdown folds single newlines): bold title, one plain sentence, sub-bullets, links."""
    paras = [f"**{title.strip()}**"]
    if sentence:
        paras.append(sentence.strip())
    if facts:
        paras.append("\n".join(f"- {f}" for f in facts))
    its = [i for i in (items or []) if i]
    if its:
        lines = _group_links(its)[:10]
        if len(its) == 1 and _same_title(title, its[0]):  # the heading already is the title: one source link, no repeated title
            nm = {"pr": "PR", "ticket": "ticket", "commit": "commit", "doc": "doc", "thread": "Slack conversation", "review": "review"}.get(its[0].type, "source")
            paras.append((f"Source: {nm} - {its[0].url}" if its[0].url else f"Source: {nm}") + (f" {FLAG}" if its[0].meta.get("confidential") else ""))
        elif len(lines) == 1 and not facts:
            one = lines[0]
            if len(its) > 1 and ": " in one and any(_same_title(title, x) for x in its):
                one = one.split(": ", 1)[1]  # the heading is the title already; the line is just the links
            paras.append(one)
        else:
            paras.append(f"{label}:\n" + "\n".join(f"- {x}" for x in lines))
    return "\n\n".join(paras)


def _inv_note(profile: dict[str, Any]) -> str:
    n = sum(1 for v in profile["invisible"] if v["label"] == "problem_solved")
    return f" ({n} listed under Problems solved)" if n else ""


def _thread_facts(ids: list[str], by_id: dict[str, Item], skip: set[str]) -> list[str]:
    out = []
    for e in ids:
        it = by_id.get(e)
        if it and it.type == "thread" and e not in skip:
            body = " ".join(l.split(": ", 1)[1] if ": " in l[:40] else l for l in it.text.splitlines())
            out.append(f"Slack, {day(it)}: {clip(re.sub(chr(32) + '+', chr(32), body).strip(), 160)}")
    return out[:2]


def _type_counts(items: list[Item]) -> list[tuple[str, int]]:
    names = {"pr": "PR", "ticket": "ticket", "commit": "commit", "thread": "Slack conversation", "doc": "doc", "review": "review"}
    c: dict[str, int] = {}
    for i in items:
        c[names.get(i.type, i.type)] = c.get(names.get(i.type, i.type), 0) + 1
    return sorted(c.items(), key=lambda kv: -kv[1])


def _span(p: dict[str, Any]) -> str:
    return p["first"] if p["first"] == p["last"] else f"{p['first']} to {p['last']}"


def _drop_unknown_keys(text: str, known: set[str]) -> str:
    """Ticket keys in model or document text that were not found as real tickets are removed (a doc can mention a key that never existed)."""
    def repl(m: re.Match) -> str:
        return m.group(0) if m.group(1) in known else ""
    t = re.sub(r"[,;]?[ \t]*\b([A-Z][A-Z0-9]+-\d+)\b", repl, text or "")
    t = re.sub(r"\b(Related|See|Refs?|Links?)\s*[:,]?\s*(?=[.;]|$)", "", t)
    return re.sub(r"\s+([.,;])", r"\1", re.sub(r"[ \t]{2,}", " ", t)).strip()


def _project_facts(p: dict[str, Any]) -> list[str]:
    facts = []
    if p.get("goal") and not _PLACEHOLDER.search(p["goal"]):
        facts.append(f"Goal: {p['goal']}")
    facts.append(f"Role: {p['role']}" + (f", with {', '.join(p['co_contributors'][:3])}" if p.get("co_contributors") else ""))
    if len(p.get("teams") or []) > 1:
        facts.append("Teams: " + ", ".join(f"{t['team']}{' (from ' + t['since'] + ')' if t['since'] else ''}" for t in p["teams"]))
    return facts


_ACTION = {"led": "I led the work", "built": "I built it", "contributed": "I contributed to it", "reviewed": "I reviewed the work", "supported": "I supported it"}


def _st_title(s: Statement, by_id: dict[str, Item]) -> str:
    it = by_id.get(s.evidence[0]) if s.evidence else None
    return short(it.title, 70) if it and it.type in {"thread", "doc", "pr", "ticket"} else short(s.text.split(":")[0], 70)


def sar_block(p: dict[str, Any], by_id: dict[str, Item], conf: set[str], skip: set[str] = frozenset()) -> str:
    """Situation, action, result for one project. Every line comes from the profile and the linked items below it; a line is dropped when the sources have nothing for it."""
    ev = _ev([e for e in p["evidence"] if e not in skip], by_id, 12)
    allev = [by_id[e] for e in p["evidence"] if e in by_id]
    start = next((i for i in sorted(allev, key=lambda i: (i.type != "ticket", i.date)) if i.type in {"ticket", "pr"}), None)
    situation = p["goal"] if p.get("goal") and not _PLACEHOLDER.search(p["goal"]) else (f"It started with {short(start.title, 90)}." if start else "")
    co = f", with {', '.join(p['co_contributors'][:3])}" if p.get("co_contributors") else ""
    mine_prs = sum(1 for i in allev if i.type == "pr" and i.meta.get("mine") and not (i.meta.get("is_revert") or i.meta.get("docs_only") or i.meta.get("generated_only")))  # revert, docs-only and lockfile PRs are not "authored work"
    action = f"{_ACTION.get(p['role'], 'I contributed to it')}{co}" + (f" and authored {mine_prs} PR{'' if mine_prs == 1 else 's'}" if mine_prs else "") + "."
    result = p["outcome"] if p.get("outcome") and not _PLACEHOLDER.search(p["outcome"]) else ""
    flag = (FLAG + " ") if any(e in conf for e in p["evidence"]) else ""
    lines = ([f"- Situation: {clip(situation, 200)}"] if situation else []) + [f"- Action: {action}"] + ([f"- Result: {clip(result, 200)}"] if result else [])
    return f"**{flag}{p['name']}**\n\n" + "\n".join(lines) + "\n\nEvidence:\n" + "\n".join(f"- {x}" for x in _group_links(ev))


def blocks_sec(heading: str, blocks: list[str], none: str) -> dict[str, Any]:
    return {"heading": heading, "body": "\n\n".join(blocks)} if blocks else {"heading": heading, "body": none}


def _drop_covered_commits(ids: list[str], by_id: dict[str, Item]) -> list[str]:
    """A commit whose key a PR or ticket in the same list already carries (or a revert next to a PR) is shown through that PR or ticket; it stays in the ledger."""
    pk = {k for i in ids if i in by_id and by_id[i].type in {"pr", "ticket"} for k in by_id[i].keys}
    has_pr = any(i in by_id and by_id[i].type == "pr" for i in ids)
    out = []
    for i in ids:
        it = by_id.get(i)
        if it and it.type == "commit" and (set(it.keys) & pk or (has_pr and it.meta.get("is_revert"))):
            continue
        out.append(i)
    return out


def _ev(ids: list[str], by_id: dict[str, Item], n: int = 3, keep_order: bool = False) -> list[Item]:
    ids = _drop_covered_commits(ids, by_id)
    if keep_order:  # the caller already ranked them (skills: the pages the user wrote first)
        return [by_id[i] for i in ids if i in by_id][:n]
    ids = sorted((i for i in ids if i in by_id), key=lambda i: (_PRI.get(by_id[i].type, 9), by_id[i].date, i))
    return [by_id[i] for i in ids[:n]]


def sources_for(items: list[Item]) -> list[dict[str, str]]:
    return [{"id": i.id, "title": i.title[:100], "url": i.url} for i in items if i.url][:300]


def build_statements(profile: dict[str, Any], by_id: dict[str, Item], kind: str, names: set[str], colleagues: list[str],
                     date_from: str = "", date_to: str = "", with_projects: bool = True) -> list[Statement]:
    """Deterministic statements from the profile, one per fact, each with evidence ids."""
    def within(ids: list[str]) -> bool:
        if not (date_from or date_to):
            return True
        ds = [day(by_id[i]) for i in ids if i in by_id]
        return any((not date_from or d >= date_from) and (not date_to or d <= date_to) for d in ds)

    sts: list[Statement] = []
    if kind in {"weekly_summary", "one_on_one", "review_material", "interview_stories"}:
        for p in (profile["projects"] if with_projects else []):
            if within(p["evidence"]):
                co = f" with {', '.join(p['co_contributors'][:3])}" if p.get("co_contributors") else ""
                tm = ""
                if len(p.get("teams") or []) > 1:
                    tm = " Teams: " + ", ".join(f"{t['team']}{' (from ' + t['since'] + ')' if t['since'] else ''}" for t in p["teams"]) + "."
                outcome = "" if _PLACEHOLDER.search(p["outcome"]) else f": {p['outcome']}"
                sts.append(Statement(f"{p['name']}{outcome}; my role: {p['role']}{co}.{tm}", p["evidence"][:6], p["role"]))
    for v in profile["invisible"]:
        if within(v["evidence"]):
            sts.append(Statement(f"{_VERB.get(v['label'], 'I helped')} with no ticket raised: {v['summary']}", v["evidence"], "supported"))
    for d in profile["decisions"]:
        if within(d["evidence"]):
            sts.append(Statement(f"Decision ({d['role']}): " + (short(d['title'], 90) + "." if _PLACEHOLDER.search(d["summary"]) else d["summary"]), d["evidence"], d["role"]))
    for a in profile["improvements"]:
        sts.append(Statement(f"Recurring feedback, {a['count']} instances: {a['label']}.", a["item_ids"][:8]))
    conf = set(profile.get("confidential", []))
    sts = [flag_confidential(s, conf) for s in sts]
    sts, _ = keep_cited(sts, set(by_id))
    sts, _ = drop_colleague_assessments(sts, colleagues)
    sts = [s for s in sts if not predicts_promotion(s.text)]
    return sts


def _first_person_win(it: Item) -> str:
    keys = [k for k in it.keys if k][:2]
    return f"I shipped {short(it.title, 80)}" + (f" ({', '.join(keys)})" if keys else "")


_VERB = {"problem_solved": "I debugged and fixed a problem", "help_given": "I helped a teammate", "mentoring": "I mentored a teammate", "decision": "I drove a technical choice"}


def _with_ticket(i: Item, by_id: dict[str, Item]) -> list[Item]:
    """The PR and the ticket it names in its title or branch, so the line links both."""
    tk = _title_key(i)
    t = next((x for x in by_id.values() if x.type == "ticket" and tk and _title_key(x) == tk), None)
    return [i] + ([t] if t else [])


def _what_it_did(i: Item, by_id: dict[str, Item]) -> str:
    """One plain sentence on what the change did, from the PR body or its ticket text. Nothing when the sources only repeat the title."""
    t = {re.sub(r"\W+", " ", x.title).lower().strip() for x in _with_ticket(i, by_id)}
    for src in _with_ticket(i, by_id):
        body = re.sub(r"\b(closes|fixes|resolves|related|refs?)\b[^.]*\.?", "", src.text or "", flags=re.I)
        body = re.sub(r"\s+", " ", re.sub(r"(^|\s)[#*_`>]+", " ", body)).strip()
        body = re.sub(r"^(?:[A-Z][A-Z0-9]+-\d+\s*[:\-]?\s*)+", "", body)  # a leading ticket key is not part of the sentence
        m = re.match(r"(.+?[.!?])(?:\s|$)", body)
        sent = (m.group(1) if m else body).strip()
        sent = sent[:1].upper() + sent[1:]
        if len(sent) >= 20 and re.sub(r"\W+", " ", sent).lower().strip() not in t:
            return " What it did: " + clip(sent, 180)
    return ""


def one_on_one(profile: dict[str, Any], by_id: dict[str, Item], sts: list[Statement]) -> list[dict[str, Any]]:
    """First-person talking points. Each item: bold title, one plain sentence, its link on its own line."""
    mine = [i for i in by_id.values() if i.meta.get("mine")]
    prs = [i for i in mine if i.type == "pr" and shipped(i) and not _TRIVIAL.search(i.title)]
    prs = sorted(prs, key=lambda i: (i.date, i.id))
    shown = prs[:8]
    wins = [block(short(i.title, 80), f"I shipped this on {day(i)}." + _what_it_did(i, by_id), items=_with_ticket(i, by_id)) for i in shown]
    if len(prs) > len(shown):
        wins.append(f"Showing {len(shown)} of {len(prs)} shipped PRs; the rest are in the item ledger.")
    if not wins:  # no shipped PRs: finished tickets, still real work
        done = [i for i in mine if i.type == "ticket" and str(i.meta.get("status", "")).lower() in {"done", "closed", "resolved"}][:3]
        wins = [block(short(i.title, 80), "I closed this ticket.", items=[i]) for i in done]
    unseen = []
    for v in profile["invisible"][:5]:
        it = by_id.get(v["evidence"][0]) if v["evidence"] else None
        if not it:
            continue
        where = f" in #{it.meta.get('channel')}" if it.meta.get("channel") else ""
        summ = re.sub(r"\s+", " ", v["summary"]).strip()
        unseen.append(block(it.title, f"{_VERB.get(v['label'], 'I helped')}{where} on {day(it)}, with no ticket raised: {summ}", items=[it]))
    decs, made = [], []
    for d in profile["decisions"][:6]:
        it = by_id.get(d["evidence"][0])
        role = "I led" if d["role"] == "led" else "I contributed to"
        summ = "" if _PLACEHOLDER.search(d["summary"]) else clip(re.sub(r"\s+", " ", d["summary"]).strip(), 200)
        open_ = bool(it and (it.meta.get("needs_decision") or NEEDS_DECISION.search(it.text or "")))  # open only when the source itself says it needs a decision
        if open_:
            decs.append(block(short(d["title"], 80), f"{role} this. {summ}".strip() + " The source says this still needs a decision.", items=[it]))
        else:
            made.append(block(short(d["title"], 80), f"{role} this. {summ}".strip(), items=[it] if it else []))
    blockers = [block(short(i.title, 80), f"This ticket was reopened {i.meta.get('reopen_count')} time(s).", items=[i]) for i in sorted(mine, key=lambda x: x.date)
                if i.type == "ticket" and i.meta.get("reopen_count", 0) > 0][:3]
    working = [block(short(i.title, 80), "My PR is still open.", items=[i]) for i in sorted(mine, key=lambda x: x.date)
               if i.type == "pr" and i.meta.get("state") == "open" and not i.meta.get("is_revert")][:3]
    working += [block(short(i.title, 80), "This ticket is in progress.", items=[i]) for i in sorted(mine, key=lambda x: x.date)
                if i.type == "ticket" and str(i.meta.get("status", "")).lower() in {"in progress", "in review", "in development"}][:3]
    for a in profile["improvements"][:2]:
        working.append(block(f"Review feedback: {a['label']}", f"I got this feedback {a['count']} times and am working on it.", items=_ev(a["item_ids"], by_id, 1)))
    gaps = [g for g in profile["gaps"] if g["status"] != "backed" and g["level"] == "next"]
    out = [{"heading": "Wins", "blocks": wins}, {"heading": "You may not have seen", "blocks": unseen}, {"heading": "Blockers", "blocks": blockers},
           {"heading": "Decisions made", "blocks": made}, {"heading": "Decision needing input", "blocks": decs}, {"heading": "Working on", "blocks": working}]
    if gaps:
        out.append({"heading": "For the next level", "blocks": [block(g["expectation"], "No evidence yet." if g["status"] == "missing" else "Evidence mapping was unavailable this run - not a finding that evidence is missing." if g["status"] == "unavailable" else f"Only {len(g['evidence'])} item(s), thin.", items=_ev(g["evidence"], by_id, 2)) for g in gaps[:4]]})
    return [{"heading": s["heading"], "body": "\n\n".join(s["blocks"])} for s in out if s["blocks"]]


def _window_work(profile: dict[str, Any], by_id: dict[str, Item], ids: set[str]) -> list[Item]:
    """Work items in the window under the same rules as the profile: shipped PRs (no reverts, no lockfile-only), commits only when no PR or ticket covers them and they are not trivial."""
    fresh = [by_id[i] for i in ids if i in by_id and by_id[i].meta.get("mine", True)]
    prs = [i for i in fresh if i.type == "pr" and shipped(i)]
    pk = {k for i in by_id.values() if i.type in {"pr", "ticket"} for k in i.keys}
    commits = [i for i in fresh if i.type == "commit" and not i.meta.get("is_revert") and not i.meta.get("reverted") and not TRIVIAL_TITLE.search(i.title)
               and not (set(i.keys) & pk)]
    return sorted(prs + commits, key=lambda x: (x.date, x.id))


def weekly_sections(profile: dict[str, Any], by_id: dict[str, Item], ids: set[str], quiet_line: str | None, window_label: str = "") -> list[dict[str, Any]]:
    """Weekly summary: what shipped (grouped by project), what is in progress, decisions, problems solved, help given. Items from the window only; every line links."""
    if quiet_line or not ids:
        return [{"heading": "Nothing new this week", "body": quiet_line or "Nothing new this week."}]
    fresh = sorted((by_id[i] for i in ids if i in by_id and by_id[i].meta.get("mine", True)), key=lambda x: (x.date, x.id))
    pof = profile.get("project_of", {})
    pname = {p["project_id"]: p["name"] for p in profile["projects"]}
    tickets = [i for i in by_id.values() if i.type == "ticket"]

    def with_tickets(group: list[Item]) -> list[Item]:
        keys = {k for i in group for k in i.keys}
        return group + [t for t in tickets if (_title_key(t) in keys or set(t.keys) & keys) and t not in group]

    work = _window_work(profile, by_id, ids)
    by_proj: dict[str, list[Item]] = {}
    for i in work:
        by_proj.setdefault(pname.get(pof.get(i.id, ""), "Other work"), []).append(i)
    shipped_blocks = ["**" + n + "**\n\n" + "\n".join(f"- {x}" for x in _group_links(with_tickets(g))[:15]) for n, g in sorted(by_proj.items(), key=lambda kv: (kv[0] == "Other work", kv[0]))]
    prog = [i for i in fresh if i.type == "pr" and not shipped(i) and not i.meta.get("reverted") and not i.meta.get("is_revert") and not i.meta.get("generated_only") and not i.meta.get("docs_only") and i.meta.get("state") != "closed"]

    def from_window(labels: set[str]) -> list[str]:
        return [block(by_id[v["evidence"][0]].title, v["summary"], items=[by_id[v["evidence"][0]]]) for v in profile["invisible"] if v["label"] in labels and v["evidence"] and v["evidence"][0] in by_id and any(e in ids for e in v["evidence"])]
    decs = [block(short(d["title"], 90), f"My role: {d['role']}." + ("" if _PLACEHOLDER.search(d["summary"]) else f" {d['summary']}"), items=_ev(d["evidence"], by_id, 1)) for d in profile["decisions"] if any(e in ids for e in d["evidence"])]
    return [
        {"heading": "What changed this week", "body": f"{len(fresh)} item(s) {window_label or 'new since the last run'}."},
        {"heading": "Shipped", "body": "\n\n".join(shipped_blocks)} if shipped_blocks else {"heading": "Shipped", "body": "Nothing shipped this week."},
        blocks_sec("In progress", ["\n".join(f"- {x}" for x in _group_links(with_tickets(prog))[:15])] if prog else [], "Nothing in progress."),
        blocks_sec("Decisions", decs, "None this week."),
        blocks_sec("Problems solved", from_window({"problem_solved"}), "None this week."),
        blocks_sec("Help given", from_window({"help_given", "mentoring"}), "None this week."),
    ]


def _empty_period(items: list[Item], date_from: str, date_to: str) -> str:
    """Nothing in the asked period: say so, and say where the saved items actually are."""
    ds = sorted(day(i) for i in items if i.date)
    if not ds or not (date_from or date_to):
        return "None found in this period."
    return f"None found in this period. The saved items are dated {ds[0]} to {ds[-1]}, so no project, thread or decision falls inside it."


def stories(profile: dict[str, Any], by_id: dict[str, Item], terms: set[str], keep_internal: bool, names=()) -> list[dict[str, Any]]:
    """Use only source-qualified actions. Cached model verdicts cannot bypass eligibility."""
    from interview import solved_text, authored_text, proposed, delivery_item, DISAGREE, ACCEPTED
    pool = []
    for v in profile["invisible"]:
        ev = [e for e in v["evidence"] if e in by_id]
        if not ev:
            continue
        it = by_id[ev[0]]
        action = solved_text(it, names)
        if v["label"] == "problem_solved" and action:
            pool.append(("A hard bug", "My action: " + action, ev, "Tell me about a hard bug you debugged."))
        own = authored_text(it, names)
        if DISAGREE.search(own) and ACCEPTED.search(own):
            pool.append(("A disagreement", "My action and resolution: " + own, ev, "Tell me about a disagreement and how you handled it."))
        if v["label"] in {"mentoring", "help_given"} and own and re.search(r"\b(?:mentored|explained|walked|paired|showed|taught|shared)\b", own, re.I):
            pool.append(("Helping a teammate", "My action: " + own, ev, "Tell me about a time you helped someone unblock."))
    for d in profile["decisions"]:
        ev = [e for e in d["evidence"] if e in by_id]
        if not ev:
            continue
        if any(proposed(by_id[e]) for e in ev):
            pool.append(("A design proposal", "I contributed this proposal; acceptance is not established by the saved evidence. " + by_id[ev[0]].title, ev, "Tell me about a design proposal you contributed to."))
            continue
        text = " ".join(by_id[e].text + " " + str(by_id[e].meta.get("dsum", "")) for e in ev)
        if not ACCEPTED.search(text) and not any(by_id[e].meta.get("decision_text") for e in ev):
            pool.append(("A design discussion", "I contributed to this design record. The saved evidence does not establish an accepted decision. " + by_id[ev[0]].title,
                         ev, "Tell me about a design discussion you contributed to."))
            continue
        trade = bool(re.search(r"\b(instead of|rather than|versus|vs\.?|trade-?offs?|alternative)\b", text, re.I))
        pool.append(("A decision with trade-offs" if trade else "A technical decision", d["summary"], ev,
                     "Tell me about a technical decision you contributed to and its trade-offs." if trade else "Tell me about a technical decision you contributed to."))
    for p in sorted(profile["projects"], key=lambda p: -len(p["evidence"])):
        ev = [e for e in p["evidence"] if e in by_id and delivery_item(by_id[e])]
        if not ev:
            continue
        role = p.get("role", "contributed")
        body = "My recorded role: " + role + ". " + (p["outcome"] if not _PLACEHOLDER.search(p["outcome"]) else "Outcome not stated.")
        if any(by_id[e].meta.get("reverted") for e in p["evidence"] if e in by_id):
            body += " Rolled-back changes are excluded from the delivery evidence below."
        pool.append(("Contributing to " + p["name"], body, ev[:6], "Tell me about your contribution to a project and its outcome."))
    out, seen = [], set()
    for title, body, ev, q in pool:
        key = (title, tuple(ev))
        if key in seen:
            continue
        seen.add(key)
        conf = any(e in set(profile.get("confidential", [])) for e in ev)
        out.append({"title": title, "story": (FLAG + " " if conf else "") + scrub(body, terms, keep_internal),
                    "question": q, "evidence": ev[:4]})
        if len(out) == 8:
            break
    return out


_FRAMEWORK_MSG = {
    "absent": "No review framework was given, so evidence gaps are skipped. A framework would enable them.",
    "unparsed": "A review framework was given, but it has no bullet or numbered lines and no model was available to read it, so evidence gaps were NOT run. Put one expectation per line starting with '-' or '1.', or run with the model enabled.",
    "failed": "A review framework was given, but the model could not extract expectations from it (see What could not be done), so evidence gaps were NOT run. Put one expectation per line starting with '-' or '1.' and run again.",
}


def corpus_for(kind: str, profile: dict[str, Any], items: list[Item], *, title: str, terms: set[str], keep_internal: bool,
               colleagues: list[str], names: set[str], date_from: str, date_to: str, run_notes: list[str],
               failed: list[dict], quiet_line: str | None, new_ids: set[str], window_label: str = "", coverage_line: str = "", coverage_status: dict | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    by_id = {i.id: i for i in items}
    known_keys = {k for i in items if i.type == "ticket" for k in i.keys}
    for d in profile["decisions"]:
        d["summary"] = _drop_unknown_keys(d["summary"], known_keys)
    for p_ in profile["projects"]:
        p_["goal"], p_["outcome"] = _drop_unknown_keys(p_["goal"], known_keys), _drop_unknown_keys(p_["outcome"], known_keys)
    s = profile["stats"]
    weekly_kind = kind in {"weekly", "weekly_summary"}
    rows, trunc = ledger.rows([i for i in items if i.id in new_ids] if weekly_kind else items, profile["project_of"], LEDGER_CAP)  # weekly: only the new rows
    pname = {p["project_id"]: p["name"] for p in profile["projects"]}
    rows = [[r[0], r[1], pname.get(r[2], "(other work)" if r[2] != "(unlinked)" else r[2]), *r[3:]] for r in rows]
    ledger_sec = {"heading": "Item ledger: every item, with source, date, project and type",
                  "body": f"{len(rows)} items" + (f" (newest {LEDGER_CAP} of {len(items)})" if trunc else "") + ". Reverts and reverted PRs are listed but not counted as shipped.",
                  "table": {"columns": ledger.COLUMNS, "rows": rows}}
    sections: list[dict[str, Any]] = []
    problems_rows: list[dict[str, Any]] = []
    sts = build_statements(profile, by_id, kind if kind not in {"backfill", "weekly"} else "profile", names, colleagues, date_from, date_to)
    if kind == "weekly_summary" or kind == "weekly":
        sections[0:0] = weekly_sections(profile, by_id, new_ids, quiet_line, window_label)

    def sec(heading: str, bullets: list[str], none: str) -> dict[str, Any]:
        return {"heading": heading, "bullets": bullets} if bullets else {"heading": heading, "body": none}
    if kind in {"backfill", "profile"}:
        mine_ = [i for i in items if i.meta.get("mine")]
        fixes = [i for i in mine_ if i.type in {"pr", "ticket"} and problem_evidence(i)
                 and (shipped(i) if i.type == "pr" else str(i.meta.get("status", "")).lower() in {"done", "closed", "resolved"})]
        slack_fixes = [v for v in profile["invisible"] if v["label"] == "problem_solved"]
        solved = [block(short(i.title, 80), problem_summary(i), items=[i]) for i in sorted(fixes, key=lambda x: x.date)[:8]]
        problems_rows = [{"title": short(i.title, 80), "summary": problem_summary(i), "date": day(i), "type": i.type, "link": _link(i)} for i in sorted(fixes, key=lambda x: x.date)[:8]]
        problems_rows += [{"title": by_id[v["evidence"][0]].title, "summary": re.sub(r"\s+", " ", v["summary"]).strip(), "date": day(by_id[v["evidence"][0]]), "type": "thread", "link": _link(by_id[v["evidence"][0]])}
                          for v in slack_fixes if v["evidence"] and v["evidence"][0] in by_id]
        def _thread_block(v: dict[str, Any]) -> str:
            it = by_id[v["evidence"][0]]
            co = [c for c in v.get("co_contributors", []) if not _SLACK_ID.fullmatch(c)][:2]
            summ = re.sub(r"\s+", " ", v["summary"]).strip()
            return block(it.title, summ, facts=[f"{day(it)}, no ticket raised" + (f", with {', '.join(co)}" if co else "")], items=[it])
        solved += [_thread_block(v) for v in slack_fixes if v["evidence"] and v["evidence"][0] in by_id]
        inv = [_thread_block(v) for v in profile["invisible"] if v["label"] != "problem_solved" and v["evidence"] and v["evidence"][0] in by_id]
        if slack_fixes:
            inv.append(f"Unticketed debugging and fixes ({len(slack_fixes)}) are listed once, under Problems solved.")
        sections += [
            blocks_sec("Projects", [block(f"{p['name']} ({_span(p)})", p["outcome"] if not _PLACEHOLDER.search(p["outcome"]) else "",
                                          facts=_project_facts(p) + _thread_facts(p["evidence"], by_id, {x for v in profile["invisible"] for x in v["evidence"]}), items=_ev([e for e in p["evidence"] if e not in {x for d in profile["decisions"] for x in d["evidence"]} and by_id[e].type != "thread"], by_id, 14) + [by_id[e] for e in p["evidence"] if e in by_id and by_id[e].type == "thread" and e not in {x for v in profile["invisible"] for x in v["evidence"]}][:2]) for p in profile["projects"]],
                       "None found: no group of two or more linked PRs, tickets or docs yet."),
            blocks_sec("Problems solved", solved, "None found: no fix or debugging work with your part in it."),
            blocks_sec("Decisions", [block(short(d["title"], 90), f"My role: {d['role']}." + ("" if _PLACEHOLDER.search(d["summary"]) else f" {d['summary']}"), items=_ev(d["evidence"], by_id, 1)) for d in profile["decisions"]],
                       "None found: no design doc, ADR or approach-chosen thread."),
            blocks_sec("Invisible work", inv, "None found: no Slack thread with your work that lacks a ticket or PR."),
            blocks_sec("Skills", [block(k["skill"], f"{k['count']} piece{'' if k['count'] == 1 else 's'} of work {'shows' if k['count'] == 1 else 'show'} this.", items=_ev(k["evidence"], by_id, 2, keep_order=True), label="For example") for k in profile["skills"]],
                       "None found: not enough evidence text to map skills."),
            blocks_sec("Improvement areas", [block(a["label"], f"This came up {a['count']} times in review feedback.", items=_ev(a["item_ids"], by_id, 3)) for a in profile["improvements"]],
                       "None found: no feedback pattern repeated 3 or more times."),
        ]
    if kind in {"backfill", "profile"}:
        shown = " ".join(str(s_.get("body", "")) + " ".join(map(str, s_.get("bullets", []) or [])) for s_ in sections)
        rest_ = [i for i in sorted(items, key=lambda x: x.date) if i.type == "thread" and i.meta.get("mine") and i.url and i.url not in shown]
        if rest_:
            sections.append(blocks_sec("Other Slack conversations", [block(i.title, clip(re.sub(r"\s+", " ", " ".join(l.split(": ", 1)[1] if ": " in l[:40] else l for l in i.text.splitlines())), 200), facts=[f"{i.date[:10]}"], items=[i]) for i in rest_], ""))
    if kind in {"backfill", "profile", "evidence_gaps"}:
        gaps = profile["gaps"]
        sections.append(blocks_sec("Evidence gaps", [block(g["expectation"], f"Level: {g['level']}. Status: {g['status']}." + (f" Framework: \u201c{g['quote']}\u201d." if g.get("quote") else "") + (" The evidence mapping was unavailable this run, so this is NOT a finding that evidence is missing (see What could not be done)." if g["status"] == "unavailable" else "" if g["evidence"] else " No items found."), items=_ev(g["evidence"], by_id, 3)) for g in gaps],
                            (_FRAMEWORK_MSG.get(profile.get("framework_status", "absent"), _FRAMEWORK_MSG["absent"]))))
    if kind == "one_on_one":
        sections = one_on_one(profile, by_id, sts)
    if kind == "review_material":
        conf_ = set(profile.get("confidential", []))
        dec_ids = {e for d in profile["decisions"] for e in d["evidence"]}
        proj_ = [sar_block(p, by_id, conf_, dec_ids) for p in profile["projects"] if (not (date_from or date_to)) or any(
            (not date_from or day(by_id[e]) >= date_from) and (not date_to or day(by_id[e]) <= date_to) for e in p["evidence"] if e in by_id)]
        other = build_statements(profile, by_id, "review_material", names, colleagues, date_from, date_to, with_projects=False)
        sections.insert(0, {"heading": f"Impact statements {date_from or 'all'} to {date_to or 'now'}",
                            "body": "\n\n".join(proj_ + [block(_st_title(s_, by_id), s_.text, items=_ev(s_.evidence, by_id, 2)) for s_ in other]) or _empty_period(items, date_from, date_to)})
    if kind == "interview_stories":
        st = stories(profile, by_id, terms, keep_internal, names)
        sections.insert(0, {"heading": "Interview stories", "sections": [{"heading": scrub(x["title"], terms, keep_internal), "body": x["story"], "bullets": [f"Answers: {x['question']}"] + [f"Evidence: [{scrub(_lab(i), terms, keep_internal)}]({i.url})" if i.url else f"Evidence: {scrub(_lab(i), terms, keep_internal)}" for i in _ev(x["evidence"], by_id, 2)]} for x in st]})
        if not keep_internal:
            linked = {i.url for x in st for i in _ev(x["evidence"], by_id, 2) if i.url and has_term(i.url, terms)} | {str(r[5]) for r in rows if r[5] and has_term(str(r[5]), terms)}
            if linked:  # the links are the evidence, so they are kept - but they carry the names the stories had stripped
                run_notes.append(f"interview stories: {len(linked)} link URL(s) (story evidence and the ledger) contain a redacted or internal name (the repo, organisation or Jira site). "
                                 "The links are kept so every story stays linked to its evidence, which means the stories are NOT safe to share as they are: strip or replace those URLs first (official AC12 check not run).")
        sections.insert(1, {"heading": "Coverage", "body": f"{len(st)} evidence-qualified story draft(s). " + (f"Below the requested 5-8: only {len(st)} eligible stories were supported by the saved evidence. No extra stories were invented. " if len(st) < 5 else "") + "Proposals are labelled as proposals; project questions do not assume end-to-end ownership. Disagreement stories require an attributed disagreement and resolution."})
    from report_notes import split_notes, FALLBACK
    info_notes, skipped_notes, limit_notes = split_notes(run_notes)
    failure_notes = [f"{f['source']}: {clip(f["error"], 420)}" for f in failed] + limit_notes
    if failure_notes:
        sections.append({"heading": "What could not be done", "bullets": list(dict.fromkeys(failure_notes))})
    if info_notes:
        sections.append({"heading": "Run info", "bullets": info_notes})
    if skipped_notes:
        sections.append({"heading": "Skipped features", "bullets": skipped_notes})
    run_notes = info_notes + skipped_notes + limit_notes
    sections = [s_ for s_ in sections if s_.get("bullets") or s_.get("table") or s_.get("body") or s_.get("sections") or s_.get("facts")]
    # The platform's CSV export is the FIRST TABLE in the corpus, so the ledger is the only table and sits last (an appendix).
    sections.append({"heading": "Appendix: item ledger (also the CSV download)",
                     "body": f"{len(rows)} items: " + ", ".join(f"{n} {t}{'' if n == 1 else 's'}" for t, n in _type_counts([i for i in items if i.id in new_ids] if weekly_kind else items)) + (f" (newest {LEDGER_CAP} of {len(items)})" if trunc else "")
                     + ". A PR, its commits and its ticket are separate rows for the same change. Reverts, reverted PRs, lockfile-only PRs and trivial commits are listed but not counted.",
                     **({"table": {"columns": ledger.COLUMNS, "rows": rows}} if rows else {})})
    from interview import proposed, ACCEPTED
    def decision_counts(records):
        established, proposals, unconfirmed = 0, 0, 0
        for record in records:
            ev = [by_id[e] for e in record["evidence"] if e in by_id]
            if any(proposed(it) for it in ev):
                proposals += 1
            elif ev and any(ACCEPTED.search(it.text + " " + str(it.meta.get("dsum", ""))) or it.meta.get("decision_text") for it in ev):
                established += 1
            else:
                unconfirmed += 1
        return established, proposals, unconfirmed
    nd, np, nu = decision_counts(profile["decisions"])
    def decision_label(d, p, u):
        return f"{d} established decision{'' if d == 1 else 's'}; {p} design proposal{'' if p == 1 else 's'}" + (f"; {u} design record(s) with acceptance unconfirmed" if u else "")
    summary_text = f"{s['prs_shipped']} PR{'' if s['prs_shipped'] == 1 else 's'} shipped, {s['reviews_given']} review{'' if s['reviews_given'] == 1 else 's'}, {len(profile['invisible'])} piece{'' if len(profile['invisible']) == 1 else 's'} of invisible work{_inv_note(profile)}, {decision_label(nd, np, nu)}. Every item below links to its source."
    if weekly_kind:
        wp = sum(1 for i in _window_work(profile, by_id, new_ids) if i.type == "pr")
        wi = sum(1 for v in profile["invisible"] if any(e in new_ids for e in v["evidence"]))
        wd, wp_, wu = decision_counts([d for d in profile["decisions"] if any(e in new_ids for e in d["evidence"])])
        summary_text = f"{wp} PR{'' if wp == 1 else 's'} shipped, {wi} piece{'' if wi == 1 else 's'} of invisible work, {decision_label(wd, wp_, wu)} in this window. Every item below links to its source."
    if coverage_line:  # the reader must see what the profile does and does not cover without opening the download
        summary_text = f"{summary_text} {coverage_line}"
    if kind == "interview_stories" and not keep_internal:  # the page title, summary and the ledger's project and title columns are part of what is shared
        title, summary_text = scrub(title, terms, False, False), scrub(summary_text, terms, False, False)
        for sec in sections:
            if sec.get("table"):
                sec["table"] = {**sec["table"], "rows": [[*r[:2], scrub(str(r[2]), terms, False, False), r[3], scrub(str(r[4]), terms, False, False), *r[5:]] for r in sec["table"]["rows"]]}
    corpus = {"title": title, "summary": summary_text,
              "sections": sections}  # no "sources" list: the page would number every item again (id, title, URL) under the report; links are in the body and the ledger table
    corpus = _clean_obj(corpus)
    result = {"title": title, "mode": kind, "stats": s, "projects": len(profile["projects"]), "invisible_work": len(profile["invisible"]),
              "decisions": nd, "design_proposals": np, "unconfirmed_design_records": nu, "decision_records": len(profile["decisions"]), "improvement_areas": [a["label"] for a in profile["improvements"]],
              "evidence_gaps": [{"expectation": g["expectation"], "status": g["status"]} for g in profile["gaps"]],
              "unavailable": list(dict.fromkeys(f["source"] for f in failed)),
              "unavailable_reasons": list({f["source"]: f.get("error", "") for f in failed}.items()), "notes": run_notes, "analysis_status": "degraded" if FALLBACK in limit_notes else "not_flagged", "ledger_rows": len(rows)}
    # Structured copies of what the page shows, as flat lists of objects, so the result page can lay them out (it can only show what is in the result object).
    def _first_link(ids: list[str]) -> str:
        return next((_link(by_id[e]) for e in ids if e in by_id and by_id[e].url), "")
    if kind in {"backfill", "profile"}:
        result["project_list"] = [{"name": p["name"], "role": p["role"], "first": p["first"], "last": p["last"], "goal": "" if _PLACEHOLDER.search(p["goal"]) else p["goal"],
                                  "outcome": "" if _PLACEHOLDER.search(p["outcome"]) else p["outcome"], "co_contributors": ", ".join(p.get("co_contributors") or []), "items": len(p["evidence"]), "link": _first_link(p["evidence"])} for p in profile["projects"]]
        result["problems_solved"] = problems_rows
        result["invisible_work_list"] = [{"label": v["label"], "summary": re.sub(r"\s+", " ", v["summary"]).strip(), "date": day(by_id[v["evidence"][0]]) if v["evidence"] and v["evidence"][0] in by_id else "", "link": _first_link(v["evidence"])}
                                         for v in profile["invisible"] if v["label"] != "problem_solved"]
        result["skills"] = [{"skill": k["skill"], "pieces_of_work": k["count"], "link": _first_link(k["evidence"])} for k in profile["skills"]]
        result["decision_list"] = [{"title": d["title"], "role": d["role"], "summary": d["summary"] if not _PLACEHOLDER.search(d["summary"]) else "", "link": _first_link(d["evidence"])} for d in profile["decisions"]]
    if weekly_kind:  # the weekly page is about this window only: what is new, as rows the page can lay out
        win = [i for i in sorted(items, key=lambda x: (x.date, x.id)) if i.id in new_ids]
        pname_ = {p_["project_id"]: p_["name"] for p_ in profile["projects"]}
        result["new_work"] = [{"date": day(i), "type": i.type, "title": short(i.title, 90), "project": pname_.get(profile["project_of"].get(i.id, ""), ""), "counted": ledger.counted_label(i), "link": _link(i)}
                              for i in win if i.type != "review"][:ROW_CAP_IN_RESULT]
        result["new_reviews"] = sum(1 for i in win if i.type == "review")
        result["new_invisible_work"] = [{"label": v["label"], "summary": re.sub(r"\s+", " ", v["summary"]).strip(), "date": day(by_id[v["evidence"][0]]) if v["evidence"] and v["evidence"][0] in by_id else "", "link": _first_link(v["evidence"])}
                                        for v in profile["invisible"] if any(e in new_ids for e in v["evidence"])]
        result["new_decisions"] = [{"title": d["title"], "role": d["role"], "link": _first_link(d["evidence"])} for d in profile["decisions"] if any(e in new_ids for e in d["evidence"])]
    result["coverage"] = dict(coverage_status or {})
    result["ledger"] = [dict(zip(ledger.COLUMNS, [str(c) for c in r])) for r in rows[:ROW_CAP_IN_RESULT]]
    result["ledger_rows_shown"] = min(len(rows), ROW_CAP_IN_RESULT)
    return corpus, result


def to_markdown(corpus: dict[str, Any], with_tables: bool = True, level: int = 2) -> str:
    """The page as plain markdown, so a local run can be read without the platform."""
    lines: list[str] = []
    if level == 2:
        lines += [f"# {corpus.get('title', '')}", "", str(corpus.get("summary", "")), ""]
    for sec in corpus.get("sections", []):
        lines += [f"{'#' * level} {sec.get('heading', '')}", ""]
        if sec.get("body"):
            lines += [str(sec["body"]), ""]
        for f in sec.get("facts", []) or []:
            lines.append(f"- {f['label']}: {f['value']} (source: {f.get('source', '')})")
        for b in sec.get("bullets", []) or []:
            lines.append(f"- {b}")
        if sec.get("table") and with_tables:
            t = sec["table"]
            lines += ["| " + " | ".join(map(str, t["columns"])) + " |", "|" + " --- |" * len(t["columns"])]
            lines += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in t["rows"]]
        if sec.get("sections"):
            lines.append(to_markdown({"sections": sec["sections"]}, with_tables, level + 1))
        lines.append("")
    return "\n".join(lines)


def write_local_copy(corpus: dict[str, Any], directory: str = ".gravity", result: dict[str, Any] | None = None) -> None:
    """Local convenience only: lets `gravity run` output be read from disk. Never touches a source; failures are ignored."""
    try:
        import csv
        import os
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "last_output.md"), "w", encoding="utf-8") as f:
            f.write(to_markdown(corpus))
        if result is not None:
            import json as _json
            with open(os.path.join(directory, "last_result.json"), "w", encoding="utf-8") as f:
                _json.dump(result, f, default=str)
        first = next((s["table"] for s in corpus.get("sections", []) if s.get("table")), None)
        with open(os.path.join(directory, "last_ledger.csv"), "w", encoding="utf-8", newline="") as f:  # rewritten every run, so it never shows a previous run's rows
            w = csv.writer(f)
            w.writerow(first["columns"] if first else ["source", "date", "project", "type", "title", "url", "counted"])
            if first:
                w.writerows(first["rows"])
    except Exception:  # noqa: BLE001
        pass
