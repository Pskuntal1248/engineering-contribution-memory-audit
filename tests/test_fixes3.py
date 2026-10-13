"""Dedup of commits against PRs across renumbered ticket keys, and evidence-based docs attribution."""
import asyncio

import pipeline
from pipeline import Config, ingest


def pr_dict(n, title, created="2026-10-10T10:00:00Z", merged="2026-10-10T12:00:00Z", files=("src/pay.py",), body=""):
    return {"_repo": "o/r", "number": n, "user": {"login": "Pskuntal1248"}, "title": title, "body": body,
            "created_at": created, "closed_at": merged, "merged_at": merged, "state": "closed",
            "files": [{"filename": f, "additions": 5, "deletions": 1} for f in files], "html_url": f"https://x/pr/{n}"}


def commit_dict(sha, msg, date="2026-10-10T11:00:00Z", files=("src/pay.py",)):
    return {"_repo": "o/r", "sha": sha, "author": {"login": "Pskuntal1248"}, "html_url": f"https://x/c/{sha}",
            "commit": {"message": msg, "author": {"date": date}},
            "files": [{"filename": f, "additions": 5, "deletions": 1} for f in files]}


def ticket_dict(key, aliases=()):
    hist = [{"created": "2026-10-09T10:00:00.000+0000", "items": [{"field": "Key", "fromString": a, "toString": key}]} for a in aliases]
    return {"key": key, "fields": {"summary": "Add idempotency key", "created": "2026-10-09T10:00:00.000+0000",
                                   "updated": "2026-10-10T10:00:00.000+0000", "status": {"name": "Done"},
                                   "assignee": {"displayName": "Parth Singh"}, "reporter": {"displayName": "Parth Singh"},
                                   "description": None},
            "changelog": {"histories": hist}}


CFG = Config(username="Pskuntal1248", repos=["o/r"], jira_projects=["PAY", "SRCH"], name_hints=["Parth Singh"])


def raw(prs, commits, jira=()):
    return {"github": {"prs": prs, "reviews": {}, "commits": commits}, "jira": list(jira),
            "slack": {"channels": [], "threads": []}, "docs": []}


def test_renumbered_key_commit_is_counted_through_its_pr():
    # the ticket was re-keyed PAY-101 -> PAY-1 (Jira changelog); the commit message still carries the old key
    out = ingest(raw([pr_dict(1, "PAY-1 Add idempotency key to payouts")],
                     [commit_dict("a" * 40, "PAY-101 Add idempotency key to payouts")],
                     [ticket_dict("PAY-1", aliases=("PAY-101",))]), CFG)
    assert [i.type for i in out.items] == ["pr", "ticket"]
    assert any("counted through its PR" in s.reason for s in out.skipped)


def test_branch_name_key_links_commit_to_pr_without_alias_record():
    # no Jira alias record, but the PR's branch carries the old key: relation evidence through the shared key
    p = pr_dict(1, "PAY-1 Add idempotency key to payouts")
    p["head"] = {"ref": "feature/PAY-101-idempotency-key"}
    out = ingest(raw([p], [commit_dict("b" * 40, "PAY-101 Add idempotency key to payouts")]), CFG)
    assert [i.type for i in out.items] == ["pr"]
    assert any("counted through its PR" in s.reason for s in out.skipped)


def test_merely_similar_title_never_merges_a_commit():
    # same words after stripping ticket keys, but no shared key, no shared files, different change: both rows stay
    out = ingest(raw([pr_dict(1, "PAY-1 Add idempotency key to payouts", files=("src/pay.py",))],
                     [commit_dict("b" * 40, "PAY-101 Add idempotency key to payouts", files=("src/other.py",))]), CFG)
    assert sorted(i.type for i in out.items) == ["commit", "pr"]


def test_same_title_outside_the_prs_life_stays_a_separate_commit():
    out = ingest(raw([pr_dict(1, "PAY-1 Add idempotency key to payouts")],
                     [commit_dict("c" * 40, "PAY-101 Add idempotency key to payouts", date="2026-12-01T11:00:00Z")]), CFG)
    assert sorted(i.type for i in out.items) == ["commit", "pr"]


def test_shadow_commit_of_a_reverted_pr_is_not_counted():
    out = ingest(raw([pr_dict(5, "SRCH-2 Add result ranking"),
                      pr_dict(11, 'Revert "SRCH-2 Add result ranking"', merged="2026-10-10T13:00:00Z")],
                     [commit_dict("d" * 40, "SRCH-202 Add result ranking")]), CFG)
    prs = {i.meta["number"]: i for i in out.items if i.type == "pr"}
    assert prs[5].meta.get("reverted") and prs[11].meta.get("is_revert")
    assert not [i for i in out.items if i.type == "commit"]  # the reverted work leaves no counted commit behind


def test_unrelated_commit_same_window_stays():
    out = ingest(raw([pr_dict(1, "PAY-1 Add idempotency key to payouts")],
                     [commit_dict("e" * 40, "Tweak logging in worker", files=("src/other.py",))]), CFG)
    assert sorted(i.type for i in out.items) == ["commit", "pr"]


# ---------------------------------------------------------------- docs attribution

def test_confluence_v2_identity_known_uses_account_id():
    from adapters import fetch_confluence
    from guards import ToolLog

    async def call(name, args):
        if name == "confluence.cql_search":
            raise RuntimeError("401 Unauthorized: scope does not match")
        if name == "confluence.get_current_user":
            return {"data": {"accountId": "acc-me", "displayName": "Parth Singh"}}
        if name == "confluence.get_pages":
            return {"data": {"results": [
                {"id": "1", "title": "Mine", "authorId": "acc-me", "createdAt": "2026-09-20T10:00:00Z",
                 "version": {"createdAt": "2026-09-21T10:00:00Z", "authorId": "acc-me"},
                 "body": {"storage": {"value": "<p>Design</p>"}}, "_links": {"webui": "/p/1"}},
                {"id": "2", "title": "Not mine", "authorId": "acc-other", "createdAt": "2026-09-20T10:00:00Z",
                 "version": {"createdAt": "2026-09-21T10:00:00Z", "authorId": "acc-other"},
                 "body": {"storage": {"value": "<p>Other</p>"}}, "_links": {"webui": "/p/2"}}],
                "_links": {"base": "https://x.atlassian.net/wiki"}}}
        if name == "confluence.get_labels_for_page":
            return {"results": []}
        raise RuntimeError("no")
    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_confluence(call, "2026-01-01T00:00:00Z", ToolLog(), out, ["Parth Singh"]))
    assert [d["title"] for d in out["docs"]] == ["Mine"]
    assert out["docs"][0]["_attribution"] == "account id from the identity call"


def test_confluence_v2_identity_refused_drops_unverifiable_pages():
    from adapters import fetch_confluence
    from guards import ToolLog
    pages = {
        "1": {"body": "<p>Design</p>", "by": "Someone Else"},
        "2": {"body": "<p>Owner: Parth Singh</p>", "by": "Someone Else"},
        "3": {"body": "<p>Nothing here</p>", "by": "Someone Else"},
    }

    async def call(name, args):
        if name in ("confluence.cql_search", "confluence.get_current_user"):
            raise RuntimeError("401 Unauthorized")
        if name == "confluence.get_pages":
            return {"data": {"results": [
                {"id": pid, "title": f"Page {pid}", "authorId": f"acc-{pid}", "createdAt": "2026-09-20T10:00:00Z",
                 "version": {"createdAt": "2026-09-21T10:00:00Z", "authorId": f"acc-{pid}"}, "_links": {"webui": f"/p/{pid}"}}
                for pid in ("1", "2", "3")], "_links": {"base": "https://x.atlassian.net/wiki"}}}
        if name == "confluence.get_page_by_id":
            b = pages[args["id"]]
            return {"id": args["id"], "body": {"storage": {"value": b["body"]}},
                    "history": {"createdBy": {"displayName": b["by"]}}, "version": {"by": {"displayName": b["by"]}}}
        if name == "confluence.get_labels_for_page":
            return {"results": []}
        raise RuntimeError("no")
    calls = []

    async def logged(name, args):
        calls.append(name)
        return await call(name, args)

    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_confluence(logged, "2026-01-01T00:00:00Z", ToolLog(), out, ["Parth Singh"]))
    # identity refused: not even the page carrying an Owner byline is opened - a byline is a claim, and finding it means reading the body
    assert out["docs"] == [] and "confluence.get_page_by_id" not in calls
    assert any("identity call was refused too" in c and "none of the 3 most recently edited pages were read" in c for c in out["capped"])


def test_notion_bodies_are_read_only_for_id_verified_pages():
    from adapters import fetch_notion
    from guards import ToolLog

    async def call(name, args):
        if name == "notion.list_users":
            return {"results": [{"object": "user", "id": "u-me", "type": "person", "name": "Parth Singh", "person": {"email": "ps.kuntal2005@gmail.com"}}]}
        if name == "notion.search_notion_page":
            return {"results": [
                {"id": "n1", "created_by": {"id": "u-me"}, "last_edited_by": {"id": "u-me"}, "last_edited_time": "2026-09-01T00:00:00Z",
                 "properties": {"Name": {"type": "title", "title": [{"plain_text": "Mine by id"}]}}},
                {"id": "n2", "created_by": {"id": "u-x"}, "last_edited_by": {"id": "u-x"}, "last_edited_time": "2026-09-01T00:00:00Z",
                 "properties": {"Name": {"type": "title", "title": [{"plain_text": "Byline page"}]}}},
                {"id": "n3", "created_by": {"id": "u-y"}, "last_edited_by": {"id": "u-y"}, "last_edited_time": "2026-09-01T00:00:00Z",
                 "properties": {"Name": {"type": "title", "title": [{"plain_text": "Unknown page"}]}}}]}
        if name == "notion.get_page_markdown":
            return {"markdown": {"n2": "Owner: Parth Singh\nbody", "n1": "design text", "n3": "other text"}[args["page_id"]]}
        raise RuntimeError("no")
    reads = []

    async def logged(name, args):
        if name == "notion.get_page_markdown":
            reads.append(args["page_id"])
        return await call(name, args)

    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_notion(logged, "2026-01-01T00:00:00Z", ToolLog(), out, ["Parth Singh", "ps.kuntal2005@gmail.com"]))
    got = {d["title"]: (d["_attribution"], d["_mine"]) for d in out["docs"]}
    # only the id-matched page is opened or kept; the byline page is never read, because finding a byline means reading the body
    assert got == {"Mine by id": ("notion account email match", True)} and reads == ["n1"]
    assert any("2 recently edited page(s) were not created or edited by your Notion user and were not read" in c for c in out["capped"])


def test_same_name_author_does_not_confer_decision_leadership():
    # a page naming "Parth Singh" with identity unresolved: kept as a claim, excluded from decisions and skills
    import normalize as N
    from analysis import find_decisions
    raw_doc = {"id": "1", "title": "Design: Search ranking approach", "body": "Owner: Parth Singh\nWe chose BM25.",
               "author": "Parth Singh", "modified": "2026-09-20T10:00:00Z", "_mine": False,
               "_attribution": "page names 'Parth Singh' as author/editor (unverified)"}
    doc = N.normalize_doc(raw_doc, "confluence", ["Parth Singh"])
    assert isinstance(doc, N.Item) and not doc.meta["mine"]
    assert "unverified" in doc.meta["attribution"]
    assert find_decisions([doc], {"Parth Singh"}, "Pskuntal1248") == []


def test_verified_doc_still_supports_a_led_decision():
    import normalize as N
    from analysis import find_decisions
    raw_doc = {"id": "2", "title": "Design: Payout reconciliation job", "body": "Owner: Parth Singh\nWe will run reconciliation nightly.",
               "author": "Parth Singh", "modified": "2026-09-20T10:00:00Z", "_mine": True,
               "_attribution": "CQL creator/contributor = currentUser() filter"}
    doc = N.normalize_doc(raw_doc, "confluence", ["Parth Singh"])
    assert doc.meta["mine"]
    ds = find_decisions([doc], {"Parth Singh"}, "Pskuntal1248")
    assert len(ds) == 1 and ds[0]["role"] == "led"  # verified authorship + the page's own Owner line


def test_exact_title_alone_does_not_fold_a_commit():
    # same words, inside the PR's life, but no shared key and no shared files: same titles can be independent work
    out = ingest(raw([pr_dict(1, "Add idempotency key to payouts", files=("src/pay.py",))],
                     [commit_dict("f" * 40, "Add idempotency key to payouts", files=("src/other.py",))]), CFG)
    assert sorted(i.type for i in out.items) == ["commit", "pr"]


def test_notion_same_name_collision_credits_nothing():
    from adapters import fetch_notion
    from guards import ToolLog

    async def call(name, args):
        if name == "notion.list_users":
            return {"results": [{"object": "user", "id": "u-1", "type": "person", "name": "Parth Singh"},
                                {"object": "user", "id": "u-2", "type": "person", "name": "Parth Singh"}]}
        if name == "notion.search_notion_page":
            return {"results": [
                {"id": "n1", "created_by": {"id": "u-1"}, "last_edited_by": {"id": "u-1"}, "last_edited_time": "2026-09-01T00:00:00Z",
                 "properties": {"Name": {"type": "title", "title": [{"plain_text": "Ambiguous"}]}}}]}
        if name == "notion.get_page_markdown":
            return {"markdown": "design text"}
        raise RuntimeError("no")
    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_notion(call, "2026-01-01T00:00:00Z", ToolLog(), out, ["Parth Singh"]))
    assert out["docs"] == []  # an ambiguous identity match credits and reads nothing
    assert any("2 Notion users match your name" in f["error"] for f in out["failed"])
    assert any("your Notion user could not be verified, so none of the 1 recent page(s) were read" in c for c in out["capped"])
