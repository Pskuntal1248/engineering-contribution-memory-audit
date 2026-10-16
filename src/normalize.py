"""Turn raw tool payloads into Items, dropping what must not count.

Pure functions. The raw shapes are the GitHub REST / Jira REST / Slack Web API
shapes the Gravity gateway passes through; every accessor is tolerant of
missing fields so one odd row is skipped and counted, not a crash.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from models import Item, Skipped

TICKET_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
URL = re.compile(r"https?://[^\s<>\)\]\"']+")
CO_AUTHOR = re.compile(r"^co-authored-by:\s*(.+?)\s*<([^>]*)>", re.I | re.M)

BOT_NAME = re.compile(r"(\[bot\]$|^dependabot|^renovate|^github-actions|^snyk|^codecov|-bot$|^bot-|^slackbot$)", re.I)

LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock", "cargo.lock",
    "gemfile.lock", "composer.lock", "go.sum", "uv.lock", "mix.lock", "pubspec.lock", "packages.lock.json",
}
GENERATED_PARTS = ("/vendor/", "/node_modules/", "/dist/", "/build/", "/generated/", "/__generated__/", "/third_party/")
GENERATED_SUFFIX = (".min.js", ".min.css", ".pb.go", "_pb2.py", ".snap", ".map", ".svg")


def parse_ts(value: Any) -> datetime | None:
    """ISO strings, epoch seconds or Slack 'secs.micros' to an aware UTC datetime."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d+(\.\d+)?", str(value)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        s = str(value).replace("Z", "+00:00")
        # Jira writes +0530 without a colon
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def iso(value: Any) -> str:
    d = parse_ts(value)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""


def is_bot(name: str | None, kind: str | None = None) -> bool:
    return bool(kind and kind.lower() == "bot") or bool(name and BOT_NAME.search(name.strip()))


def is_excluded_file(path: str) -> bool:
    """Lockfile, generated or vendored: never counted toward size."""
    p = "/" + path.lower().lstrip("/")
    base = p.rsplit("/", 1)[-1]
    return base in LOCKFILES or base.endswith(".lock") or any(g in p for g in GENERATED_PARTS) or p.endswith(GENERATED_SUFFIX)


def ticket_keys(*texts: str, projects: Iterable[str] | None = None) -> list[str]:
    allowed = {p.strip().upper() for p in projects} if projects else None
    found: list[str] = []
    for t in texts:
        for m in TICKET_KEY.findall(t or ""):
            if allowed is None or m.split("-")[0] in allowed:
                if m not in found:
                    found.append(m)
    return found


def urls_in(*texts: str) -> list[str]:
    out: list[str] = []
    for t in texts:
        for u in URL.findall(t or ""):
            u = u.rstrip(".,;")
            if u not in out:
                out.append(u)
    return out


def _login(u: Any) -> str:
    if isinstance(u, dict):
        return str(u.get("login") or u.get("name") or "")
    return str(u or "")


# ---------------------------------------------------------------- GitHub
_LOCK_WORDS = re.compile(r"lock[\s-]?files?\b|package-lock|yarn\.lock|poetry\.lock|pnpm-lock|go\.sum|uv\.lock|cargo\.lock|gemfile\.lock", re.I)


def _fname(f: dict[str, Any]) -> str:
    return str(f.get("filename") or f.get("path") or f.get("name") or f.get("file") or "")


_DOCISH = re.compile(r"(^|/)(docs?|adrs?|rfcs?|decisions?)/|\.(md|rst|txt|adoc)$", re.I)
_DOC_TITLE = re.compile(r"^\s*(docs?|documentation)(\([^)]*\))?\s*:|\bADR-\d+|\bRFC[- ]?\d*\b", re.I)


def docs_only_pr(raw_files: list[dict[str, Any]] | None, title: str) -> bool:
    """A PR that only adds or edits documentation or an ADR/RFC: a proposal, not shipped code."""
    names = [_fname(f) for f in (raw_files or []) if isinstance(f, dict) and _fname(f)]
    if names:
        return all(_DOCISH.search(n) for n in names)
    return bool(_DOC_TITLE.search(title or ""))


def _generated_only(raw: dict[str, Any], size: dict[str, int], title: str, body: str) -> bool:
    """True when the PR changes only lockfiles/generated files. Uses the file list; when the list is missing or empty
    (lookup failed or capped) falls back to the title and body saying lockfile."""
    files = [f for f in (raw.get("files") or []) if isinstance(f, dict)]
    names = [_fname(f) for f in files if _fname(f)]
    if names:
        return all(is_excluded_file(n) for n in names)
    return bool(_LOCK_WORDS.search(title or "") or (_LOCK_WORDS.search((body or "")[:500]) and re.search(r"\bonly\b", (title or "") + " " + (body or "")[:500], re.I)))


def pr_size(files: list[dict[str, Any]] | None, additions: int, deletions: int) -> dict[str, int]:
    """Changed lines excluding lockfile/generated/vendored files. With no file list we
    can only trust the PR totals, and say so (`size_basis`)."""
    if files:
        kept = [f for f in files if not is_excluded_file(_fname(f))]
        a = sum(int(f.get("additions", 0) or 0) for f in kept)
        d = sum(int(f.get("deletions", 0) or 0) for f in kept)
        total = sum(int(f.get("additions", 0) or 0) + int(f.get("deletions", 0) or 0) for f in files)
        return {"lines": a + d, "excluded": total - (a + d), "files": len(kept), "basis": "files"}
    return {"lines": int(additions or 0) + int(deletions or 0), "excluded": 0, "files": 0, "basis": "totals"}


def revert_target(title: str, body: str) -> str | None:
    """If this PR reverts another, return a hint: '#123' or the quoted original title."""
    t = (title or "").strip()
    m = re.match(r'^revert\b[:\s]*"?(.*?)"?$', t, re.I)
    if not m:
        return None
    n = re.search(r"#(\d+)", body or "") or re.search(r"reverts?\s+(?:pull request\s+)?#(\d+)", body or "", re.I)
    return f"#{n.group(1)}" if n else (m.group(1) or "").strip() or ""


def pr_merged_at(raw: dict[str, Any]) -> str:
    """Merge time from whichever field the gateway fills: merged_at, pull_request.merged_at, or a merged flag plus closed_at."""
    pr = raw.get("pull_request") if isinstance(raw.get("pull_request"), dict) else {}
    got = iso(raw.get("merged_at") or pr.get("merged_at"))
    if got:
        return got
    if raw.get("merged") is True or pr.get("merged") is True:
        return iso(raw.get("closed_at") or pr.get("closed_at"))
    return ""


def normalize_pr(raw: dict[str, Any], repo: str, username: str, projects: Iterable[str] | None = None) -> Item | Skipped:
    num = raw.get("number")
    if num is None:
        return Skipped(str(raw.get("id", "?")), "pr without number")
    author = _login(raw.get("user") or raw.get("author"))
    iid = f"gh:pr:{repo}#{num}"
    if is_bot(author, (raw.get("user") or {}).get("type") if isinstance(raw.get("user"), dict) else None):
        return Skipped(iid, "bot author")
    created = iso(raw.get("created_at"))
    if not created:
        return Skipped(iid, "no created_at")
    merged = pr_merged_at(raw)
    closed = iso(raw.get("closed_at"))
    body = raw.get("body") or ""
    title = raw.get("title") or ""
    head = (raw.get("head") or {}).get("ref", "") if isinstance(raw.get("head"), dict) else str(raw.get("head") or "")
    size = pr_size(raw.get("files"), raw.get("additions", 0), raw.get("deletions", 0))
    rev = revert_target(title, body)
    labels = [(l.get("name") if isinstance(l, dict) else str(l)) for l in raw.get("labels", [])]
    co = [m[0].strip() for m in CO_AUTHOR.findall(body)]
    cd = parse_ts(merged or closed)
    cr = parse_ts(created)
    return Item(
        id=iid, source="github", type="pr", date=merged or created, title=title,
        url=raw.get("html_url") or raw.get("url") or "", text=body[:4000], author=author, co_authors=co,
        keys=ticket_keys(head, title, body, projects=projects),
        refs=urls_in(body),
        meta={
            "repo": repo, "number": num, "created": created, "merged": merged, "closed": closed,
            "merged_flag": bool(merged), "state": raw.get("state", ""), "size": size, "branch": head,
            "cycle_hours": round((cd - cr).total_seconds() / 3600, 1) if cd and cr and merged else None,
            "is_revert": rev is not None, "reverts": rev, "labels": labels,
            "generated_only": _generated_only(raw, size, title, body),
            "docs_only": docs_only_pr(raw.get("files"), title),  # lockfile / generated / vendored files only
            "paths": [_fname(f) for f in (raw.get("files") or []) if isinstance(f, dict) and _fname(f)][:15],
            "merged_basis": raw.get("_merged_basis", "merged_at" if merged else ""),
            "mine": author.lower() == username.lower() or username.lower() in [c.lower() for c in co],
        },
    )


def mark_reverted(prs: list[Item]) -> None:
    """A PR that a later revert PR points at is reverted: listed, not shipped."""
    by_num = {p.meta["number"]: p for p in prs if p.type == "pr"}
    by_title = {p.title.strip().lower(): p for p in prs if p.type == "pr" and not p.meta.get("is_revert")}
    for p in prs:
        if p.type != "pr" or not p.meta.get("is_revert"):
            continue
        hint = p.meta.get("reverts") or ""
        target = None
        if hint.startswith("#") and hint[1:].isdigit():
            target = by_num.get(int(hint[1:]))
        if target is None and hint:
            target = by_title.get(hint.strip().lower())
        if target is not None and target is not p:
            target.meta["reverted"] = True
            target.meta["reverted_by"] = p.id


def normalize_review(raw: dict[str, Any], repo: str, pr_number: int, pr_author: str, pr_url: str,
                     username: str, projects: Iterable[str] | None = None) -> Item | Skipped:
    rid = raw.get("id")
    iid = f"gh:review:{repo}#{pr_number}:{rid}"
    reviewer = _login(raw.get("user"))
    if is_bot(reviewer):
        return Skipped(iid, "bot reviewer")
    when = iso(raw.get("submitted_at") or raw.get("created_at"))
    if not when:
        return Skipped(iid, "no submitted_at")
    body = raw.get("body") or ""
    mine = reviewer.lower() == username.lower()
    return Item(
        id=iid, source="github", type="review", date=when, title=f"Review on {repo}#{pr_number} ({raw.get('state','')})",
        url=raw.get("html_url") or (f"{pr_url}#pullrequestreview-{rid}" if pr_url else ""), text=body[:2000], author=reviewer, keys=ticket_keys(body, projects=projects),
        meta={"repo": repo, "number": pr_number, "state": raw.get("state", ""), "pr_author": pr_author,
              "mine": mine, "received": (pr_author.lower() == username.lower() and not mine)},
    )


def normalize_commit(raw: dict[str, Any], repo: str, username: str, projects: Iterable[str] | None = None) -> Item | Skipped:
    sha = raw.get("sha", "")
    iid = f"gh:commit:{repo}@{sha[:10]}"
    c = raw.get("commit") or {}
    msg = c.get("message") or raw.get("message") or ""
    author = _login(raw.get("author")) or (c.get("author") or {}).get("name", "")
    if is_bot(author):
        return Skipped(iid, "bot author")
    when = iso((c.get("author") or {}).get("date") or raw.get("date"))
    if not sha or not when:
        return Skipped(iid, "no sha or date")
    co = [m[0].strip() for m in CO_AUTHOR.findall(msg)]
    files = raw.get("files")
    size = pr_size(files, (raw.get("stats") or {}).get("additions", 0), (raw.get("stats") or {}).get("deletions", 0))
    first = msg.splitlines()[0] if msg else ""
    paths = [f.get("filename", "") for f in (files or []) if isinstance(f, dict) and f.get("filename") and not is_excluded_file(f.get("filename", ""))][:15]
    mine = author.lower() == username.lower() or any(username.lower() in x.lower() for x in co)
    return Item(
        id=iid, source="github", type="commit", date=when, title=first[:200], url=raw.get("html_url", ""),
        text=msg[:2000], author=author, co_authors=co, keys=ticket_keys(msg, projects=projects),
        meta={"repo": repo, "sha": sha, "size": size, "mine": mine, "paths": paths, "is_revert": first.lower().startswith("revert")},
    )


_REVERTS_SHA = re.compile(r"this reverts commit ([0-9a-f]{7,40})", re.I)


def mark_reverted_commits(commits: list[Item]) -> None:
    """A commit that a later 'This reverts commit <sha>' points at is reverted: listed, not counted."""
    by_sha = {c.meta.get("sha", ""): c for c in commits if c.type == "commit"}
    for c in commits:
        if c.type != "commit" or not c.meta.get("is_revert"):
            continue
        m = _REVERTS_SHA.search(c.text)
        if not m:
            continue
        pre = m.group(1).lower()
        for sha, target in by_sha.items():
            if sha.lower().startswith(pre) and target is not c:
                target.meta["reverted"] = True
                target.meta["reverted_by"] = c.id


# ------------------------------------------------------------------ Jira
def normalize_ticket(raw: dict[str, Any], site: str, username_hints: Iterable[str]) -> Item | Skipped:
    key = raw.get("key")
    if not key:
        return Skipped(str(raw.get("id", "?")), "ticket without key")
    f = raw.get("fields") or {}
    hints = {h.lower() for h in username_hints if h}

    def person(p: Any) -> str:
        return str((p or {}).get("displayName") or (p or {}).get("emailAddress") or (p or {}).get("name") or "") if isinstance(p, dict) else str(p or "")

    assignee, reporter = person(f.get("assignee")), person(f.get("reporter"))
    created, updated = iso(f.get("created")), iso(f.get("updated"))
    if not created:
        return Skipped(key, "no created")
    hist = ((raw.get("changelog") or {}).get("histories")) or []
    reopen, transitions, key_aliases = 0, [], []
    for h in sorted(hist, key=lambda x: x.get("created", "")):
        for it in h.get("items", []):
            if it.get("field") == "status":
                transitions.append((iso(h.get("created")), it.get("fromString"), it.get("toString")))
                if (it.get("fromString") or "").lower() in {"done", "closed", "resolved"} and (it.get("toString") or "").lower() not in {"done", "closed", "resolved"}:
                    reopen += 1
            if it.get("field") == "Key" and it.get("fromString"):
                key_aliases.append(it["fromString"])
    from adapters import adf_text
    desc = adf_text(f.get("description"))
    parent = f.get("parent") if isinstance(f.get("parent"), dict) else {}
    epic = parent.get("key") or (f.get("epic") or {}).get("key") if isinstance(f.get("epic"), dict) else parent.get("key")
    comments = [c for c in ((f.get("comment") or {}).get("comments") or [])]
    mine_comment = any(person(c.get("author")).lower() in hints for c in comments)
    for old in raw.get("_aliases", []):  # the ticket was moved: its old key still resolves to this one
        key_aliases.append(old)
    text = (desc or "")[:3000]
    return Item(
        id=f"jira:{key}", source="jira", type="ticket", date=iso(f.get("resolutiondate")) or updated or created,
        title=f"{key}: {f.get('summary', '')}", url=f"{site.rstrip('/')}/browse/{key}" if site else "", text=text,
        author=reporter, keys=[key] + ([epic] if epic else []) + ticket_keys(desc), refs=urls_in(desc, *[adf_text(c.get("body")) for c in comments]),
        meta={"project": key.split("-")[0], "status": (f.get("status") or {}).get("name", "") if isinstance(f.get("status"), dict) else str(f.get("status") or ""),
              "assignee": assignee, "reporter": reporter, "created": created, "reopen_count": reopen,
              "transitions": len(transitions), "aliases": key_aliases, "type_name": (f.get("issuetype") or {}).get("name", "") if isinstance(f.get("issuetype"), dict) else "",
              "mine": bool(raw.get("_mine")) or assignee.lower() in hints or reporter.lower() in hints or mine_comment,
              "confidential": bool(re.search(r"confidential", " ".join(str(l) for l in f.get("labels", [])), re.I))},
    )


# ----------------------------------------------------------------- Slack
_DANGLING = {"the", "a", "an", "in", "on", "of", "to", "for", "and", "or", "with", "at", "by", "is", "are", "was", "that", "this", "my", "our", "from", "as", "it", "we", "i", "so", "but"}


def thread_title(text: str, n: int = 110) -> str:
    """A complete first sentence or clause; when too long it is cut at a word with an ellipsis, never ending on a dangling word or ticket key."""
    line = next((l.strip() for l in str(text).splitlines() if l.strip()), "")
    line = re.sub(r"\s+", " ", line)
    m = re.match(r"(.+?[.!?])(?:\s|$)", line)
    if len(line) > n and m and not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+\.", m.group(1)) and len(m.group(1)) >= 12:
        line = m.group(1)  # too long as a whole: the first sentence, when it is complete and fits
    line = line.rstrip(" ,;:-")
    if len(line) <= n:
        return line
    words = line[:n].rsplit(" ", 1)[0].split()
    while words and (words[-1].lower().strip(",;:") in _DANGLING or re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", words[-1].strip(",;:."))):
        words.pop()
    return " ".join(words).rstrip(" ,;:-.") + "..."


def normalize_thread(channel: str, channel_id: str, messages: list[dict[str, Any]], user_ids: set[str],
                     permalink_base: str = "") -> Item | Skipped:
    """A Slack thread (or lone message) becomes one item; bots and join/leave noise are dropped."""
    msgs = [m for m in messages if not m.get("bot_id") and m.get("subtype") not in {"bot_message", "channel_join", "channel_leave"}
            and not is_bot(m.get("username"))]
    if not msgs:
        return Skipped(f"slack:{channel_id}", "only bot or system messages")
    msgs.sort(key=lambda m: float(m.get("ts", 0)))
    root = msgs[0]
    ts = str(root.get("ts"))
    iid = f"slack:{channel_id}:{ts}"
    mine = [m for m in msgs if m.get("user") in user_ids]
    if not mine:
        return Skipped(iid, "user did not take part")
    first_mine = re.sub(r"\s+", " ", str(mine[0].get("text", ""))).strip()
    title = thread_title(str(mine[0].get("text", ""))) or f"#{channel.lstrip('#')} thread"
    text = "\n".join(f"{m.get('user_name') or m.get('user')}: {m.get('text','')}" for m in msgs)[:4000]
    # a real permalink when Slack gave one; else built from the workspace address; else Slack's own redirect, which opens the message
    url = root.get("permalink") or (f"{permalink_base}/archives/{channel_id}/p{ts.replace('.', '')}" if permalink_base
                                    else f"https://slack.com/app_redirect?channel={channel_id}&message_ts={ts}")
    return Item(
        id=iid, source="slack", type="thread", date=iso(root.get("ts")), title=title, url=url, text=text,
        author=str(root.get("user_name") or root.get("user") or ""), keys=ticket_keys(text), refs=urls_in(text),
        meta={"channel": channel.lstrip("#"), "channel_id": channel_id, "messages": len(msgs), "my_messages": len(mine),
              "participants": sorted({str(m.get("user_name") or m.get("user")) for m in msgs}), "mine": True,
              "root_is_mine": root.get("user") in user_ids,
              "my_names": sorted({str(m.get("user_name")) for m in mine if m.get("user_name")})},  # display names behind the user's own slack_user_ids: provably the user
    )


# ------------------------------------------------------------------ Docs
CONF_WORDS = re.compile(r"\b(confidential|do not share|internal only|restricted|nda)\b", re.I)


_SYSTEM_TITLE = re.compile(r"^(template\s*[-:\u2013]|overview$|home$|welcome\b|getting started\b|sample\b|untitled\b|how[- ]to guide$|troubleshooting article$)", re.I)
_BOILERPLATE = re.compile(r"welcome to (your|the|this) [\w\s]{0,40}(space|site|team)|this is (the|your) (home|overview) page|use this (page|template)|get started with confluence|"
                          r"(edit|customi[sz]e) this page|click (edit|here) to|quick links to|add a (short )?description|space (home|overview)|replace this (text|description)", re.I)


def _system_page(title: str, body: str, body_failed: bool = False) -> bool:
    """Pages a tool generates for every space (templates, overview/home, getting-started guides) and pages with no real content."""
    t = re.sub(r"\s+", " ", (title or "")).strip()
    b = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body or "")).strip()
    if _SYSTEM_TITLE.search(t):
        return True
    if len(b) < 12:
        return not body_failed  # a body that could not be read is not an empty page
    return bool(_BOILERPLATE.search(b[:600])) and len(b) < 700


def normalize_doc(raw: dict[str, Any], source: str, username_hints: Iterable[str], projects: Iterable[str] | None = None) -> Item | Skipped:
    did = raw.get("id")
    if not did:
        return Skipped("?", "doc without id")
    title = raw.get("title") or ""
    body = raw.get("body") or raw.get("content") or ""
    labels = [str(x) for x in raw.get("labels", [])]
    hint_names = {h.lower() for h in username_hints if h}
    if not title.strip() or title.strip().lower().startswith("untitled"):
        return Skipped(f"doc:{source}:{did}", "page has no title")
    if title.strip().lower() in hint_names:
        return Skipped(f"doc:{source}:{did}", "profile page, not work")
    url_ = str(raw.get("url") or "")
    if re.search(r"/(?:spaces/[^/]+/)?overview/?(?:[?#].*)?$", url_) or str(raw.get("kind", "")).lower() in {"space", "homepage", "space home"}:
        return Skipped(f"doc:{source}:{did}", "space home page (overview), not a page you wrote")
    if _system_page(title, body, bool(raw.get("body_failed"))):
        return Skipped(f"doc:{source}:{did}", "sample, template or space overview page generated by the tool, not your work")
    conf = bool(raw.get("confidential")) or bool(CONF_WORDS.search(title + " " + " ".join(labels) + " " + body))  # the whole fetched body, title and labels: a marker late in a long page counts
    hints = {h.lower() for h in username_hints if h}
    author = str(raw.get("author") or "")
    editors = [str(e) for e in raw.get("editors", [])]
    named = author.lower() in hints or any(e.lower() in hints for e in editors)
    mine = bool(raw.get("_mine"))  # a matching name on the page is a claim, not verified identity: it never confers credit on its own
    return Item(
        id=f"doc:{source}:{did}", source="doc", type="doc", date=iso(raw.get("modified") or raw.get("created")), title=title,
        url=raw.get("url", ""), text=body[:4000], author=author, keys=ticket_keys(title, body, projects=projects), refs=urls_in(body),
        meta={"doc_source": source, "confidential": conf, "mine": mine, "labels": labels, "body_read": bool(body.strip()) and not raw.get("body_failed"),
              "kind": raw.get("kind", ""),
              "attribution": str(raw.get("_attribution") or ("named on the page as author/editor (unverified)" if named and not mine else ""))},
    )


def dedupe(items: Iterable[Item]) -> tuple[list[Item], int]:
    """First occurrence wins; same id or same non-empty url is a duplicate."""
    seen_id: set[str] = set()
    seen_url: set[str] = set()
    out: list[Item] = []
    dropped = 0
    for it in items:
        if it.id in seen_id or (it.url and it.url in seen_url):
            dropped += 1
            continue
        seen_id.add(it.id)
        if it.url:
            seen_url.add(it.url)
        out.append(it)
    return out, dropped

