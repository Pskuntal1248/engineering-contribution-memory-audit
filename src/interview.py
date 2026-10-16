"""Interview eligibility depends on attributed source evidence, never just cached model labels."""
from __future__ import annotations
import re

RESOLVED = re.compile(r"\b(I|we)\s+(?:have\s+)?(?:fixed|traced|resolved|patched|cleared|found|reproduced|debugged)\b|\b(?:fixed|resolved|patched|root cause|workaround)\b", re.I)
QUESTION = re.compile(r"\b(?:not sure|how (?:do|can|to)|need help|keep getting|can you|could you)\b", re.I)
DISAGREE = re.compile(r"\b(?:disagreed|disagreement|pushed back|objected|challenged)\b", re.I)
ACCEPTED = re.compile(r"\b(?:agreed|resolved|accepted|decided|compromis(?:e|ed))\b", re.I)
PROPOSED = re.compile(r"\b(?:status\s*:\s*(?:proposed|draft)|proposes?|proposed|needs? a decision|awaiting approval|pending approval)\b", re.I)


def authored_text(it, names=()):
    if it.meta.get('interview_action'):
        return str(it.meta['interview_action'])
    known = {str(n).strip().casefold() for n in names if n}
    if not known and it.meta.get("mine"):
        known.add(it.author.strip().casefold())
    lines = [line.split(': ', 1) for line in it.text.splitlines() if ': ' in line]
    if lines:
        mine = [body for author, body in lines if author.strip().casefold() in known]
        return ' '.join(mine)
    return it.text if it.meta.get('mine') and it.author.strip().casefold() in known else ''


def solved_text(it, names=()):
    t = authored_text(it, names)
    parts = re.split(r'(?<=[.!?])\s+', t)
    safe = [p.strip() for p in parts if RESOLVED.search(p) and not QUESTION.search(p)]
    if not re.search(r"\b(?:fixed|resolved|patched|cleared|workaround)\b", t, re.I):
        return ""
    return ' '.join(safe)


def proposed(it):
    return bool(it.meta.get('needs_decision') or it.meta.get('interview_proposed') or PROPOSED.search(it.text))


def delivery_item(it):
    from metrics import shipped
    if it.meta.get('reverted') or it.meta.get('is_revert'):
        return False
    if it.type == 'pr':
        return shipped(it)
    if it.type == 'ticket':
        return str(it.meta.get('status', '')).lower() in {'done', 'closed', 'resolved'}
    return False
