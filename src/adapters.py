"""Read-only adapters for Jira, Confluence, Notion, Google Drive and Slack search, on Gravity's gateway tools.
Each returns plain rows in the shape normalize.py expects, and names any failure instead of hiding it.
Argument names come from the gateway's own tool catalog (gravity tools); response shapes are parsed defensively
and are NOT verified against a connected account until a real run."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

from guards import ToolLog, err_text

Call = Callable[[str, dict], Awaitable[Any]]
PARALLEL = 6
BACKOFF = 1.5
DOC_PARALLEL = 2  # Notion, Confluence and Drive tools hit rate limits when called many at once
MAX_TICKETS = 600           # 3x the spec's 200
MAX_TICKET_LOOKUPS = 600
MAX_DOCS_PER_SOURCE = 120   # 3x the spec's 40
MAX_SLACK_HITS = 1500
MAX_THREAD_FETCHES = 600

JIRA_FIELDS = ["summary", "status", "assignee", "reporter", "created", "updated", "resolutiondate", "issuetype", "parent", "labels", "description", "project", "comment"]


def rows(data: Any, *keys: str, _depth: int = 0) -> list[dict]:
    """The list of records in a gateway payload. Tools wrap their result in different envelopes (data, response_data,
    results...), so named keys are tried first and then any nested envelope, a few levels deep."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and _depth < 5:
        for k in (*keys, "items", "data", "results", "values"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                got = rows(v, *keys, _depth=_depth + 1)
                if got:
                    return got
        for k, v in data.items():  # unknown envelope: look inside it, skipping bookkeeping keys
            if isinstance(v, dict) and k not in {"properties", "metadata", "fields"}:
                got = rows(v, *keys, _depth=_depth + 1)
                if got:
                    return got
    return []


def find_key(data: Any, *names: str, _depth: int = 0) -> Any:
    """First value stored under any of `names`, searching nested envelopes."""
    if isinstance(data, dict) and _depth < 5:
        for n in names:
            if data.get(n) not in (None, ""):
                return data[n]
        for v in data.values():
            if isinstance(v, dict):
                got = find_key(v, *names, _depth=_depth + 1)
                if got not in (None, ""):
                    return got
    return None


def adf_text(node: Any) -> str:
    """Jira descriptions and comments may be Atlassian Document Format. Plain text out, no markup."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(t for t in (adf_text(n) for n in node) if t)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        parts = [adf_text(node.get("content"))] if node.get("content") else []
        if node.get("type") == "inlineCard":
            parts.append(str((node.get("attrs") or {}).get("url", "")))
        return "\n".join(p for p in parts if p)
    return ""


def _tool_msg(err: str) -> str:
    """The readable part of a tool error: the JSON "message" when there is one, else the text, cut at a word."""
    from guards import clip
    m = re.search(r'"message"\s*:\s*"([^"]+)"', err or "")
    return clip((m.group(1) if m else err or "").strip(), 100)


def explain(source: str, err: str) -> str:
    """A reason a person can act on, from a tool error."""
    e = (err or "").lower()
    tm = _tool_msg(err or "")
    name = source.split(":")[-1].capitalize()
    if any(k in e for k in ("not enabled", "not installed", "no confluence", "not licensed", "unavailable", "no such app", "not available")):
        return f"{name} is not enabled on this site or workspace (tool said: {tm}). Remove it from docs_source or enable it."
    if "scope" in e and any(k in e for k in ("401", "403", "unauthor", "forbidden")):
        return f"{name} is connected but its permission scope does not match; reconnect with: gravity connect {source.split(':')[-1].lower()}"
    if any(k in e for k in ("needs_reconnect", "reconnect", "unauthor", "401", "403", "forbidden", "not connected", "no connection", "no account")):
        return f"{name} is not connected, or its connection needs renewing. Connect {name} and run again to include it (tool said: {tm})"
    if "404" in e or "not found" in e:
        return f"{name} was not found at that site (tool said: {tm})"
    return f"{name} could not be read: {tm}"


def failed_payload(data: Any) -> str:
    """The text of an error returned as data (success false / error key) instead of raised, else empty."""
    if isinstance(data, dict):
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        if data.get("successful") is False or data.get("success") is False or inner.get("successful") is False:
            return str(data.get("error") or inner.get("error") or data.get("message") or "the tool reported failure")
        if isinstance(data.get("error"), (str, dict)) and data.get("error") and not any(k in data for k in ("results", "values", "issues")):
            return str(data["error"])
    return ""


def text_of(data: Any, depth: int = 0) -> str:
    """Body text from whatever shape a docs tool returns, with HTML tags removed."""
    if data is None or depth > 6:
        return ""
    if isinstance(data, str):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", data)).strip() if "<" in data else data
    if isinstance(data, list):
        return " ".join(t for t in (text_of(x, depth + 1) for x in data) if t)
    if isinstance(data, dict):
        for k in ("markdown", "content", "text", "value", "body", "storage", "view", "data"):
            if k in data:
                got = text_of(data[k], depth + 1)
                if got:
                    return got
        return " ".join(t for t in (text_of(v, depth + 1) for k, v in data.items() if k not in {"id", "url", "_links"}) if t)
    return ""


def docs_text(data: Any) -> str:
    """Plain text of a Google Docs API document: the textRun contents of body.content, in order. Falls back to nothing, never to metadata."""
    out: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            run = x.get("textRun")
            if isinstance(run, dict) and isinstance(run.get("content"), str):
                out.append(run["content"])
            for k, v in x.items():
                if k != "textRun":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    body = None
    if isinstance(data, dict):
        body = data.get("body") or find_key(data, "body")
    walk(body if body is not None else data)
    return re.sub(r"\s+", " ", "".join(out)).strip()


_NO_RETRY = ("time limit", "401", "403", "404", "scope", "unauthor", "forbidden", "not enabled", "not found", "not connected", "permission")


async def _gather(sem: asyncio.Semaphore, call: Call, name: str, args: dict) -> Any:
    async with sem:
        try:
            return await call(name, args)
        except Exception as e:  # noqa: BLE001 - one retry for a transient gateway failure, never for a permission or missing-item error
            if any(k in err_text(e).lower() for k in _NO_RETRY):
                raise
            await asyncio.sleep(BACKOFF)
            return await call(name, args)


# ------------------------------------------------------------------ Jira
async def fetch_jira(call: Call, projects: list[str], since: str, log: ToolLog, out: dict) -> None:
    """Tickets for already-verified project keys. GatewaySource._preflight checks the keys before this is ever called."""
    if not projects:
        out["failed"].append({"source": "jira", "error": "no Jira project keys given"})
        return
    jql = (f'project in ({",".join(projects)}) AND updated >= "{since[:10]}" '
           f'AND (assignee = currentUser() OR reporter = currentUser() OR commentedBy = currentUser()) ORDER BY updated DESC')
    found: list[dict] = []
    token: str | None = None
    try:
        for _ in range(12):
            args: dict[str, Any] = {"jql": jql, "max_results": 100, "fields": JIRA_FIELDS}
            if token:
                args["next_page_token"] = token
            log.add("jira.search_issues", projects=projects)
            data = await call("jira.search_issues", args)
            found += rows(data, "issues")
            token = find_key(data, "next_page_token", "nextPageToken")
            if not token or len(found) >= MAX_TICKETS or find_key(data, "isLast") is True:
                break
    except Exception as e:  # noqa: BLE001
        out["failed"].append({"source": "jira", "error": explain("jira", err_text(e, 200))})
        if not found:
            return
    if len(found) >= MAX_TICKETS:
        out["capped"].append(f"jira tickets: first {MAX_TICKETS} only")
    sem = asyncio.Semaphore(PARALLEL)

    async def detail(n: int, t: dict) -> None:
        t["_mine"] = True  # the query only returns tickets assigned to, created by or commented on by the connected user
        key = t.get("key")
        if not key or n >= MAX_TICKET_LOOKUPS:
            return
        try:
            log.add("jira.get_issue", key=key)
            full = await _gather(sem, call, "jira.get_issue", {"issue_id_or_key": key, "expand": "changelog", "fields": JIRA_FIELDS})
            if isinstance(full, dict):
                full = full.get("issue", full)
                if isinstance(full, dict):
                    if full.get("fields"):
                        t["fields"] = {**(t.get("fields") or {}), **full["fields"]}
                    if full.get("changelog"):
                        t["changelog"] = full["changelog"]
                    if full.get("key") and full["key"] != key:
                        t["key"] = full["key"]
                        t.setdefault("_aliases", []).append(key)
        except Exception as e:  # noqa: BLE001 - the ticket is still listed, without history
            out["failed"].append({"source": f"jira-history:{key}", "error": err_text(e, 120)})
            return
        c = (t.get("fields") or {}).get("comment") or {}
        have = c.get("comments") or []
        total = c.get("total", len(have))
        if total > len(have):  # the issue carries only some comments: page the rest
            try:
                got: list[dict] = list(have)
                while len(got) < min(total, 300):
                    log.add("jira.list_issue_comments", key=key)
                    page = await _gather(sem, call, "jira.list_issue_comments", {"issue_id_or_key": key, "start_at": len(got), "max_results": 100})
                    more = rows(page, "comments")
                    if not more:
                        break
                    got += more
                t["fields"]["comment"] = {"comments": got, "total": total}
            except Exception:  # noqa: BLE001
                pass

    await asyncio.gather(*(detail(n, t) for n, t in enumerate(found[:MAX_TICKETS])))
    out["jira"] = found[:MAX_TICKETS]


# ------------------------------------------------------------ Confluence

def _scope_problem(err: str) -> bool:
    e = (err or "").lower()
    return "scope" in e or "401" in e or "403" in e or "unauthor" in e or "forbidden" in e


_PROBES = (("confluence.who_am_i", {}), ("confluence.get_spaces", {"limit": 1}), ("confluence.search_pages", {"limit": 1}), ("confluence.get_pages", {"limit": 1}))


async def probe_confluence(call: Call, log: ToolLog) -> str:
    """When every read path is refused, try one tiny read per other Confluence tool so the message says which ones the connection allows."""
    res = []
    for name, args in _PROBES:
        try:
            log.add(name)
            data = await call(name, args)
            bad = failed_payload(data)
            res.append(f"{name.split('.')[1]}: " + ("refused" if bad else "ok"))
        except Exception:  # noqa: BLE001
            res.append(f"{name.split('.')[1]}: refused")
    return "Checked what the connection allows: " + ", ".join(res) + "."


async def fetch_confluence_v2(call: Call, since: str, log: ToolLog, out: dict, first_error: str, names: list[str] | None = None) -> None:
    """Fallback when the CQL search (Confluence REST v1) is refused for scope: list recent pages metadata-first through the
    v2 pages tool, then open only pages the identity call proves the connected account created or edited. If the identity call
    is refused too, no page is opened at all - an explicit coverage limit, not a body-reading name hunt."""
    mine_ids: set[str] = set()
    my_name = ""
    try:  # who the connection is; some connections refuse this call while the pages list still works
        log.add("confluence.get_current_user")
        me = await call("confluence.get_current_user", {})
        acct = None if failed_payload(me) else find_key(me, "account_id", "accountId")
        mine_ids = {str(acct)} if acct else set()
        my_name = str(find_key(me, "display_name", "displayName", "public_name", "publicName") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        log.add("confluence.get_pages")
        data = await call("confluence.get_pages", {"sort": "-modified-date", "limit": 100, "status": "current"})
        if failed_payload(data):
            raise RuntimeError(failed_payload(data))
    except Exception as e:  # noqa: BLE001
        out["failed"].append({"source": "docs:confluence", "error": explain("docs:confluence", err_text(e, 400)).rstrip(". ") + ". " + await probe_confluence(call, log)})
        return
    bad = failed_payload(data)
    if bad:
        out["failed"].append({"source": "docs:confluence", "error": explain("docs:confluence", bad).rstrip(". ") + ". " + await probe_confluence(call, log)})
        return
    listed = rows(data, "results")
    if not mine_ids:  # identity refused: without account ids, verifying authorship would mean reading every page body to hunt
        # for a name - that is exactly the broad read the spec does not authorize. Title-count only, explicit coverage limit.
        out["capped"].append(f"confluence: read through the pages list (the CQL search was refused); the identity call was refused too, "
                             f"so none of the {len(listed)} most recently edited pages were read - contribution could not be verified. "
                             "Fix the Confluence connection's scopes to restore docs coverage.")
        return
    sem = asyncio.Semaphore(DOC_PARALLEL)

    async def labels_of(pid: str) -> list[str]:
        try:
            log.add("confluence.get_labels_for_page", id=pid)
            got = await _gather(sem, call, "confluence.get_labels_for_page", {"id": pid, "limit": 50})
            return [str(x.get("name", "")) for x in rows(got, "results")]
        except Exception:  # noqa: BLE001
            return []

    async def detail(pid: str) -> dict:
        """The v1 page read carries the body and the author/editor display names the v2 list omits."""
        try:
            log.add("confluence.get_page_by_id", id=pid)
            page = await _gather(sem, call, "confluence.get_page_by_id", {"id": pid})
            return page if isinstance(page, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    skipped = 0
    for h in listed[:MAX_DOCS_PER_SOURCE]:
        ver = h.get("version") or {}
        modified = str(ver.get("createdAt") or h.get("createdAt") or "")
        if modified[:10] and modified[:10] < since[:10]:
            continue
        author_id, editor_id = str(h.get("authorId") or ""), str(ver.get("authorId") or "")
        if author_id not in mine_ids and editor_id not in mine_ids:
            skipped += 1
            continue  # not created or edited by the connected account: never opened
        page = await detail(str(h.get("id", "")))  # body and display names are fetched only for verified pages
        body = text_of((page or {}).get("body") or {})
        hist, pver = (page or {}).get("history") or {}, (page or {}).get("version") or {}
        author_name = str((hist.get("createdBy") or {}).get("displayName") or "")
        editor_name = str((pver.get("by") or {}).get("displayName") or "")
        links = h.get("_links") or {}
        base = str((find_key(data, "base") or "")).rstrip("/")
        web = str(links.get("webui") or "")
        out["docs"].append({
            "_source": "confluence", "id": str(h.get("id", "")), "title": h.get("title") or "", "body": body,
            "labels": await labels_of(str(h.get("id", ""))), "author": author_name or my_name,
            "editors": [editor_name] if editor_name else ([my_name] if my_name else []),
            "created": str(h.get("createdAt") or ""), "modified": modified, "url": (base + web) if web.startswith("/") and base else web,
            "_mine": True, "_attribution": "account id from the identity call", "kind": "confluence page"})
    note = "confluence: read through the pages list (the CQL search was refused); only the 100 most recently edited pages were checked"
    if skipped:
        note += f"; {skipped} page(s) not created or edited by your account were not read"
    out.setdefault("capped", []).append(note)


async def fetch_confluence(call: Call, since: str, log: ToolLog, out: dict, names: list[str] | None = None) -> None:
    cql = f'type = page AND (creator = currentUser() OR contributor = currentUser()) AND lastModified >= "{since[:10]}" ORDER BY lastModified DESC'
    try:
        log.add("confluence.cql_search")
        data = await call("confluence.cql_search", {"cql": cql, "limit": 50, "expand": "content.version,content.history,content.metadata.labels"})
    except Exception as e:  # noqa: BLE001
        if _scope_problem(str(e)):
            await fetch_confluence_v2(call, since, log, out, str(e), names)
            return
        out["failed"].append({"source": "docs:confluence", "error": explain("docs:confluence", err_text(e, 400))})
        return
    bad = failed_payload(data)
    if bad:
        if _scope_problem(bad):
            await fetch_confluence_v2(call, since, log, out, bad, names)
            return
        out["failed"].append({"source": "docs:confluence", "error": explain("docs:confluence", bad)})
        return
    hits = rows(data, "results")[:MAX_DOCS_PER_SOURCE]
    sem = asyncio.Semaphore(DOC_PARALLEL)

    async def one(h: dict) -> None:
        c = h.get("content") if isinstance(h.get("content"), dict) else h
        pid = str(c.get("id") or h.get("id") or "")
        if not pid:
            return
        hist = c.get("history") or {}
        ver = c.get("version") or {}
        labels = [str(l.get("name", "")) for l in rows(((c.get("metadata") or {}).get("labels")) or {}, "results")]
        body = ""
        try:
            log.add("confluence.get_page_by_id", id=pid)
            page = await _gather(sem, call, "confluence.get_page_by_id", {"id": pid})
            body = text_of((page or {}).get("body") if isinstance(page, dict) and "body" in page else page)
        except Exception as e:  # noqa: BLE001
            out["failed"].append({"source": f"docs:confluence:{pid}", "error": err_text(e, 120)})
        links = c.get("_links") or h.get("_links") or {}
        out["docs"].append({
            "_source": "confluence", "id": pid, "title": c.get("title") or h.get("title") or "", "body": body, "labels": labels,
            "author": ((hist.get("createdBy") or {}).get("displayName")) or "", "editors": [((ver.get("by") or {}).get("displayName")) or ""],
            "created": hist.get("createdDate") or "", "modified": h.get("lastModified") or ver.get("when") or hist.get("createdDate") or "",
            "url": h.get("url") or links.get("webui") or "", "_mine": True, "_attribution": "CQL creator/contributor = currentUser() filter", "kind": "confluence page"})

    await asyncio.gather(*(one(h) for h in hits))
    if len(hits) >= MAX_DOCS_PER_SOURCE:
        out["capped"].append(f"confluence pages: first {MAX_DOCS_PER_SOURCE} only")


# ---------------------------------------------------------------- Notion
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


async def notion_user_id(call: Call, names: list[str], log: ToolLog, out: dict) -> str:
    """The connected user's Notion id, proven ONLY by an exact account-email match: the user puts their Notion account
    email in display_name, and exactly one workspace person must carry that email. A display name is a claim any namesake
    shares, so a name match never resolves identity - it only shapes the message explaining the gap."""
    try:
        log.add("notion.list_users")
        users = rows(await call("notion.list_users", {"page_size": 100}), "results", "users")
    except Exception as e:  # noqa: BLE001
        out["failed"].append({"source": "docs:notion", "error": f"could not list Notion users to find yours: {err_text(e, 120)}"})
        return ""
    people = [u for u in users if str(u.get("type", "person")) == "person" and str(u.get("object", "user")) == "user"]
    emails = {m.group(0).lower() for n in names for m in EMAIL_RE.finditer(n or "")}
    if emails:
        hits = [u for u in people if str(((u.get("person") or {}).get("email")) or "").lower() in emails]
        if len(hits) == 1:  # account emails are unique identifiers: this is positive evidence, not a name claim
            return str(hits[0].get("id", ""))
        if len(hits) > 1:
            out["failed"].append({"source": "docs:notion", "error": "more than one Notion user carries your account email, so authorship cannot be verified"})
            return ""
    want = [n.lower() for n in names if n]

    def label(u: dict) -> str:
        return " ".join([str(u.get("name") or ""), str(((u.get("person") or {}).get("email")) or "")]).lower()

    matches = [u for u in people if any(w and (w in label(u) or label(u).split("@")[0].strip() == w) for w in want)]
    if len(matches) == 1:
        out["failed"].append({"source": "docs:notion", "error": "a Notion user matches your name, but a matching name is not proof of identity: set display_name to your Notion account email to confirm it"})
    elif len(matches) > 1:  # a same-name collision is not identity evidence
        out["failed"].append({"source": "docs:notion", "error": f"{len(matches)} Notion users match your name, so authorship cannot be verified: set display_name to your Notion account email"})
    elif people:
        out["failed"].append({"source": "docs:notion", "error": f"could not tell which of {len(people)} Notion users is you: set display_name to your Notion account email"})
    return ""


async def fetch_notion(call: Call, since: str, log: ToolLog, out: dict, names: list[str] | None = None) -> None:
    """Recent pages. The API forces a broad LIST (no author filter exists), but not broad READS: a page's body is fetched
    only after its creator/editor id matches the resolved Notion user. Everything else is never opened, and counted."""
    names = names or []
    me = await notion_user_id(call, names, log, out)
    pages: list[dict] = []
    cursor: str | None = None
    try:
        for _ in range(3):
            args: dict[str, Any] = {"query": "", "filter_value": "page", "filter_property": "object", "timestamp": "last_edited_time", "direction": "descending", "page_size": 100}
            if cursor:
                args["start_cursor"] = cursor
            log.add("notion.search_notion_page")
            data = await call("notion.search_notion_page", args)
            batch = rows(data, "results")
            pages += batch
            cursor = find_key(data, "next_cursor")
            if not cursor or not batch or str(batch[-1].get("last_edited_time", "9999")) < since:
                break
    except Exception as e:  # noqa: BLE001
        out["failed"].append({"source": "docs:notion", "error": err_text(e, 200)})
        return

    def uid(x: Any) -> str:
        return str((x or {}).get("id", "")) if isinstance(x, dict) else ""

    recent = [p for p in pages if str(p.get("last_edited_time", "")) >= since][:MAX_DOCS_PER_SOURCE]
    if not me:  # no verified user id: reading bodies to hunt for name evidence is not authorized - nothing is opened
        if recent:
            out["capped"].append(f"notion: your Notion user could not be verified, so none of the {len(recent)} recent page(s) were read. "
                                 "Set display_name to your Notion account email to restore docs coverage.")
        return
    sem = asyncio.Semaphore(DOC_PARALLEL)
    unverified = 0

    def title(p: dict) -> str:
        for v in (p.get("properties") or {}).values():
            if isinstance(v, dict) and v.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in v.get("title", []) if isinstance(t, dict))
        return str(p.get("title") or "")

    async def one(p: dict) -> None:
        nonlocal unverified
        cb, eb = uid(p.get("created_by")), uid(p.get("last_edited_by"))
        if me not in (cb, eb):
            unverified += 1
            return  # creator/editor ids come with the listing; no match means the body is never fetched
        body, body_failed = "", False
        try:
            log.add("notion.get_page_markdown", id=p.get("id"))
            body = text_of(await _gather(sem, call, "notion.get_page_markdown", {"page_id": p.get("id")}))
        except Exception as e:  # noqa: BLE001 - the page is still listed from its title; only the preview is missing
            body_failed = True
            out["failed"].append({"source": f"docs:notion:{title(p) or p.get('id')}", "error": "page text could not be read, so it is listed by title only (" + err_text(e) + ")"})
        out["docs"].append({"_source": "notion", "id": p.get("id"), "title": title(p), "body": body, "labels": [],
                            "author": names[0] if (cb == me and names) else "", "editors": [names[0]] if (eb == me and names) else [],
                            "created": p.get("created_time", ""), "modified": p.get("last_edited_time", ""), "url": p.get("url", ""),
                            "_mine": True, "_attribution": "notion account email match", "kind": "notion page", "body_failed": body_failed})

    await asyncio.gather(*(one(p) for p in recent))
    if unverified:
        out["capped"].append(f"notion: {unverified} recently edited page(s) were not created or edited by your Notion user and were not read")


# ----------------------------------------------------------- Google Drive
MAX_DRIVE_REVISION_CHECKS = 20  # revision listing costs one call per unresolved doc; bound it like every other detail read


def _same_account(person: Any, me: dict) -> bool:
    """Email addresses decide. Display names are claims, and a blank side (anonymous edits leave the field empty) proves nothing."""
    a = str((person or {}).get("emailAddress") or "").lower()
    b = str(me.get("emailAddress") or "").lower()
    return bool(a) and bool(b) and a == b


async def drive_identity(call: Call, log: ToolLog, out: dict) -> dict:
    """The connected Drive account, from its own about record. No record, no credit: without it no doc can be tied to the user."""
    try:
        log.add("drive.get_about")
        data = await call("drive.get_about", {"fields": "user"})
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            user = find_key(data, "user")
        if isinstance(user, dict) and (user.get("emailAddress") or user.get("displayName")):
            return user
        out["failed"].append({"source": "docs:drive", "error": "the connected Drive account could not be identified from its about record, so no document is read or credited"})
    except Exception as e:  # noqa: BLE001
        out["failed"].append({"source": "docs:drive", "error": "the connected Drive account could not be identified, so no document is read or credited: " + err_text(e, 160)})
    return {}


async def fetch_drive(call: Call, since: str, log: ToolLog, out: dict) -> None:
    """Docs the connected account wrote or edited. The query only pre-filters by permission (owner/writer), and permission is
    not contribution: a body is exported only with positive edit evidence - the account last modified the doc, in the report period, or it has an in-period revision
    by the account. Everything else is listed by title, never read, never credited."""
    me = await drive_identity(call, log, out)
    q = ("trashed = false and mimeType = 'application/vnd.google-apps.document' "
         f"and modifiedTime > '{since[:10]}T00:00:00' and ('me' in owners or 'me' in writers)")
    try:
        log.add("drive.find_file")
        data = await call("drive.find_file", {"q": q, "pageSize": 100, "orderBy": "modifiedTime desc",
                                              "fields": "files(id,name,mimeType,createdTime,modifiedTime,webViewLink,description,owners(displayName,emailAddress),lastModifyingUser(displayName,emailAddress))"})
    except Exception as e:  # noqa: BLE001
        out["failed"].append({"source": "docs:drive", "error": err_text(e, 200)})
        return
    files = rows(data, "files")[:MAX_DOCS_PER_SOURCE]
    sem = asyncio.Semaphore(DOC_PARALLEL)
    unverified = 0
    revision_checks = 0
    history_gaps = 0

    async def edited(f: dict) -> bool:
        nonlocal revision_checks, history_gaps
        if not me:
            return False
        if _same_account(f.get("lastModifyingUser"), me):
            return True
        if revision_checks >= MAX_DRIVE_REVISION_CHECKS:
            return False
        revision_checks += 1
        try:  # Docs coalesces and prunes revisions: history can prove an older edit, but its absence proves nothing
            log.add("drive.list_revisions", id=f.get("id"))
            data = await _gather(sem, call, "drive.list_revisions", {"fileId": f.get("id"), "pageSize": 200,
                                                                     "fields": "revisions(id,modifiedTime,lastModifyingUser(displayName,emailAddress))"})
        except Exception:  # noqa: BLE001
            history_gaps += 1
            return False
        revs = rows(data, "revisions")
        if not revs:
            history_gaps += 1
        # only an in-period revision counts: an old edit must not credit a doc whose recent work is someone else's
        return any(_same_account(r.get("lastModifyingUser"), me) and str(r.get("modifiedTime") or "")[:10] >= since[:10] for r in revs)

    async def one(f: dict) -> None:
        nonlocal unverified
        owner = (f.get("owners") or [{}])[0]
        base = {"_source": "drive", "id": f.get("id"), "title": f.get("name", ""), "labels": [],
                "author": owner.get("displayName") or owner.get("emailAddress") or "",
                "editors": [(f.get("lastModifyingUser") or {}).get("displayName") or ""],
                "created": f.get("createdTime", ""), "modified": f.get("modifiedTime", ""), "url": f.get("webViewLink", ""), "kind": "google doc"}
        if not await edited(f):
            unverified += 1
            out["docs"].append({**base, "body": "", "_mine": False, "body_failed": True,
                                "_attribution": "access only - owning or being able to edit is not contribution; no edit evidence found, so the body was not read"})
            return
        body, body_failed = "", False
        try:
            log.add("drive.get_document", id=f.get("id"))
            body = docs_text(await _gather(sem, call, "drive.get_document", {"document_id": f.get("id")}))  # export only returns a download link, never text
            if not body:
                body_failed = True
                out["failed"].append({"source": f"docs:drive:{f.get('name') or f.get('id')}", "error": "the document came back with no text, so it is listed by title only"})
        except Exception as e:  # noqa: BLE001
            body_failed = True
            out["failed"].append({"source": f"docs:drive:{f.get('name') or f.get('id')}", "error": "doc text could not be read, so it is listed by title only (" + err_text(e) + ")"})
        out["docs"].append({**base, "body": (body + " " + (f.get("description") or "")).strip(),
                            "_mine": True, "body_failed": body_failed,
                            "_attribution": "edit evidence: the connected account last modified the doc in the report period or owns an in-period revision"})

    await asyncio.gather(*(one(f) for f in files))
    if unverified:
        out["capped"].append(f"drive: {unverified} doc(s) you can access had no proof you wrote or edited them in the report period - listed by title only, bodies not read, not credited")
    if history_gaps:
        out["capped"].append(f"drive: edit history was missing or unreadable for {history_gaps} doc(s); genuine older edits may exist that cannot be proven")


# ------------------------------------------------------- Slack by search
def _matches(data: Any) -> list[dict]:
    got = find_key(data, "matches")
    if isinstance(got, list):
        return [x for x in got if isinstance(x, dict)]
    return rows(data, "matches", "messages")


async def fetch_slack_search(call: Call, picked: list[dict], since_ts: str, log: ToolLog, out: dict) -> None:
    """The user's own messages in the chosen channels only, then each thread they appear in. `picked` has already
    passed the DM guard; a hit from any other channel is dropped here too."""
    from guards import is_dm_channel
    by_id = {c["id"]: c for c in picked}
    after = since_ts[:10]
    threads: dict[tuple[str, str], None] = {}
    for ch in picked:
        name = str(ch.get("name") or "").lstrip("#")
        if not name or is_dm_channel(ch):
            continue  # DMs (opt-in) are read through history below, never through a name search
        try:
            log.add("slack.search_messages", channel=ch["id"], is_dm=False)
            data = await call("slack.search_messages", {"query": f"in:#{name} from:me after:{after}", "count": 100, "auto_paginate": True, "sort": "timestamp", "sort_dir": "asc"})
        except Exception as e:  # noqa: BLE001
            out["failed"].append({"source": f"slack-search:#{name}", "error": err_text(e, 200)})
            continue
        hits = _matches(data)[:MAX_SLACK_HITS]
        if len(hits) >= MAX_SLACK_HITS:
            out["capped"].append(f"slack hits in #{name}: first {MAX_SLACK_HITS} only")
        for h in hits:
            cid = str((h.get("channel") or {}).get("id") or h.get("channel_id") or ch["id"]) if isinstance(h.get("channel"), dict) else str(h.get("channel") or ch["id"])
            if cid != ch["id"] or cid not in by_id:
                continue  # not the chosen channel
            if h.get("user"):
                out["slack"].setdefault("my_user_ids", [])
                if h["user"] not in out["slack"]["my_user_ids"]:
                    out["slack"]["my_user_ids"].append(h["user"])  # from:me hits are the connected user's own messages
            mb = re.match(r"(https://[^/]+)/archives/", str(h.get("permalink") or ""))
            if mb:
                out["slack"]["permalink_base"] = mb.group(1)
            ts = str(h.get("thread_ts") or "")
            if not ts:  # search hits may only carry the thread in the permalink (?thread_ts=...); a reply must fetch its whole thread
                mt = re.search(r"thread_ts=(\d+\.\d+)", str(h.get("permalink") or ""))
                ts = mt.group(1) if mt else str(h.get("ts") or "")
            if ts:
                threads[(cid, ts)] = None
    sem = asyncio.Semaphore(PARALLEL)
    keys = list(threads)[:MAX_THREAD_FETCHES]
    if len(threads) > len(keys):
        out["capped"].append(f"slack threads: first {MAX_THREAD_FETCHES} only")

    async def one(cid: str, ts: str) -> None:
        try:
            log.add("slack.fetch_message_thread_from_a_conversation", channel=cid, is_dm=False)
            data = await _gather(sem, call, "slack.fetch_message_thread_from_a_conversation", {"channel": cid, "ts": ts, "limit": 200})
            msgs = rows(data, "messages")
        except Exception as e:  # noqa: BLE001
            out["failed"].append({"source": f"slack-thread:{cid}:{ts}", "error": err_text(e, 120)})
            return
        if msgs:
            out["slack"]["threads"].append({"channel": by_id[cid].get("name", ""), "channel_id": cid, "messages": msgs})

    await asyncio.gather(*(one(c, t) for c, t in keys))

