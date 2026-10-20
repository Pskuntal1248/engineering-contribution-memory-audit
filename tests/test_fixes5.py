"""Batch 5: document-handling corrections - bodies are read only with positive edit evidence.

Drive: owner/writer permission is not contribution; a body export needs lastModifyingUser == the
connected account or a revision owned by it. Notion: a page body is fetched only after its
creator/editor id matches the resolved user. Confluence v2: pages are opened only when the
identity call ties their author/editor account id to the connected account; if identity is
refused, nothing is opened. Unknown history is an explicit coverage limit, never false credit."""

import asyncio

from readers import fetch_confluence, fetch_drive, fetch_notion
from guards import ToolLog

SINCE = "2026-01-01T00:00:00Z"
ME = {"emailAddress": "me@example.com", "displayName": "Parth Singh"}


def doc(fid, name, owner_email="me@example.com", owner_name="Parth Singh", lm_email="me@example.com", lm_name="Parth Singh"):
    return {"id": fid, "name": name, "mimeType": "application/vnd.google-apps.document",
            "createdTime": "2026-09-01T10:00:00Z", "modifiedTime": "2026-09-20T10:00:00Z",
            "webViewLink": f"https://docs.google.com/document/d/{fid}/edit",
            "owners": [{"displayName": owner_name, "emailAddress": owner_email}],
            "lastModifyingUser": ({"displayName": lm_name, "emailAddress": lm_email} if lm_name is not None else {})}


def drive_call(files, about=None, about_error=None, revisions=None, revisions_error=None, export_text="body text"):
    exports, revision_calls = [], []

    async def call(name, args):
        if name == "drive.get_about":
            if about_error:
                raise RuntimeError(about_error)
            return about if about is not None else {"user": dict(ME)}
        if name == "drive.find_file":
            return {"files": files}
        if name == "drive.list_revisions":
            revision_calls.append(args["fileId"])
            if revisions_error:
                raise RuntimeError(revisions_error)
            return revisions if revisions is not None else {"revisions": []}
        if name == "drive.get_document":
            exports.append(args["document_id"])
            return {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": export_text}}]}}]}}
        raise RuntimeError(f"unexpected tool {name}")

    return call, exports, revision_calls


def run_drive(call):
    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_drive(call, SINCE, ToolLog(), out))
    return out


def test_drive_last_modified_by_me_is_read_and_credited():
    call, exports, revs = drive_call([doc("d1", "Mine")])
    out = run_drive(call)
    assert exports == ["d1"] and revs == []  # lastModifyingUser already proves it; no history call needed
    assert out["docs"][0]["_mine"] is True and "edit evidence" in out["docs"][0]["_attribution"]


def test_drive_revision_by_me_proves_an_older_genuine_edit():
    revs = {"revisions": [
        {"id": "1", "modifiedTime": "2026-09-20T10:00:00Z", "lastModifyingUser": {"displayName": "Someone Else", "emailAddress": "other@example.com"}},
        {"id": "2", "modifiedTime": "2026-09-15T10:00:00Z", "lastModifyingUser": dict(ME)}]}
    call, exports, rev_calls = drive_call([doc("d1", "Edited long ago", lm_email="other@example.com", lm_name="Someone Else")], revisions=revs)
    out = run_drive(call)
    assert rev_calls == ["d1"] and exports == ["d1"]
    assert out["docs"][0]["_mine"] is True


def test_drive_owning_a_doc_without_edit_evidence_reads_nothing():
    revs = {"revisions": [{"id": "1", "modifiedTime": "2026-09-19T10:00:00Z", "lastModifyingUser": {"displayName": "Someone Else", "emailAddress": "other@example.com"}}]}
    call, exports, rev_calls = drive_call([doc("d1", "I only own it", lm_email="other@example.com", lm_name="Someone Else")], revisions=revs)
    out = run_drive(call)
    assert rev_calls == ["d1"] and exports == []  # never opened
    d = out["docs"][0]
    assert d["_mine"] is False and d["body"] == "" and d["body_failed"] is True
    assert "access only" in d["_attribution"]
    assert any("no proof you wrote or edited them" in c for c in out["capped"])


def test_drive_preperiod_revision_by_me_grants_no_credit():
    # the doc changed hands inside the period; my only edit predates it, so the recent work is not mine
    revs = {"revisions": [
        {"id": "1", "modifiedTime": "2025-06-01T10:00:00Z", "lastModifyingUser": dict(ME)},
        {"id": "2", "modifiedTime": "2026-09-20T10:00:00Z", "lastModifyingUser": {"displayName": "Someone Else", "emailAddress": "other@example.com"}}]}
    call, exports, _ = drive_call([doc("d1", "Edited long ago by me", lm_email="other@example.com", lm_name="Someone Else")], revisions=revs)
    out = run_drive(call)
    assert exports == [] and out["docs"][0]["_mine"] is False
    assert any("no proof you wrote or edited them in the report period" in c for c in out["capped"])


def test_drive_same_display_name_different_email_credits_nothing():
    # a namesake, on the file and inside its history, is a claim - not identity
    revs = {"revisions": [{"id": "1", "modifiedTime": "2026-09-19T10:00:00Z", "lastModifyingUser": {"displayName": "Parth Singh", "emailAddress": "impostor@example.com"}}]}
    call, exports, _ = drive_call(
        [doc("d1", "Namesake's doc", owner_email="impostor@example.com", lm_email="impostor@example.com")], revisions=revs)
    out = run_drive(call)
    assert exports == [] and out["docs"][0]["_mine"] is False


def test_drive_refused_identity_call_reads_nothing():
    call, exports, rev_calls = drive_call([doc("d1", "Mine"), doc("d2", "Also mine")], about_error="403 insufficient scopes")
    out = run_drive(call)
    assert exports == [] and rev_calls == []  # without the connected account's identity nothing can be verified
    assert all(d["_mine"] is False and d["body"] == "" for d in out["docs"])
    assert any("could not be identified" in f["error"] for f in out["failed"])


def test_drive_unreadable_revision_history_is_a_stated_gap_not_a_credit():
    call, exports, _ = drive_call(
        [doc("d1", "Old doc", lm_email="other@example.com", lm_name="Someone Else")], revisions_error="404 revisions gone")
    out = run_drive(call)
    assert exports == [] and out["docs"][0]["_mine"] is False
    assert any("edit history was missing or unreadable for 1 doc(s)" in c and "genuine older edits may exist" in c for c in out["capped"])


def test_drive_revision_checks_are_capped():
    revs = {"revisions": [{"id": "1", "modifiedTime": "2026-09-19T10:00:00Z", "lastModifyingUser": {"displayName": "Someone Else", "emailAddress": "other@example.com"}}]}
    files = [doc(f"d{i}", f"Doc {i}", lm_email="other@example.com", lm_name="Someone Else") for i in range(25)]
    call, exports, rev_calls = drive_call(files, revisions=revs)
    out = run_drive(call)
    assert len(rev_calls) == 20 and exports == [] and len(out["docs"]) == 25


def notion_call(users, pages):
    markdown_reads = []

    async def call(name, args):
        if name == "notion.list_users":
            return {"results": users}
        if name == "notion.search_notion_page":
            return {"results": pages, "next_cursor": None}
        if name == "notion.get_page_markdown":
            markdown_reads.append(args["page_id"])
            return "page text"
        raise RuntimeError(f"unexpected tool {name}")

    return call, markdown_reads


def page(pid, creator, editor, title="Page"):
    return {"id": pid, "object": "page", "created_time": "2026-09-01T10:00:00Z", "last_edited_time": "2026-09-20T10:00:00Z",
            "created_by": {"id": creator}, "last_edited_by": {"id": editor}, "url": f"https://notion.so/{pid}",
            "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}}}


def run_notion(call, names=("Parth Singh", "me@example.com")):
    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_notion(call, SINCE, ToolLog(), out, list(names)))
    return out


def test_notion_page_by_someone_else_is_never_opened():
    users = [{"object": "user", "type": "person", "id": "u-me", "name": "Parth Singh", "person": {"email": "me@example.com"}},
             {"object": "user", "type": "person", "id": "u-other", "name": "Someone Else"}]
    pages = [page("p-mine", "u-me", "u-me", "Mine"), page("p-theirs", "u-other", "u-other", "Theirs")]
    call, reads = notion_call(users, pages)
    out = run_notion(call)
    assert reads == ["p-mine"]  # the other page's body is never fetched
    assert [d["title"] for d in out["docs"]] == ["Mine"]
    assert any("1 recently edited page(s) were not created or edited by your Notion user and were not read" in c for c in out["capped"])


def test_notion_unverified_user_reads_nothing():
    users = [{"object": "user", "type": "person", "id": "u-other", "name": "Someone Else"}]
    call, reads = notion_call(users, [page("p1", "u-other", "u-other")])
    out = run_notion(call)
    assert reads == [] and out["docs"] == []
    assert any("your Notion user could not be verified, so none of the 1 recent page(s) were read" in c for c in out["capped"])


def test_notion_unique_name_match_without_email_resolves_nothing():
    # one visible namesake is possible: a name is a claim, so without an account-email match nothing is opened or credited
    users = [{"object": "user", "type": "person", "id": "u-me", "name": "Parth Singh"}]
    call, reads = notion_call(users, [page("p1", "u-me", "u-me")])
    out = run_notion(call, names=("Parth Singh",))
    assert reads == [] and out["docs"] == []
    assert any("a matching name is not proof of identity" in f["error"] for f in out["failed"])
    assert any("your Notion user could not be verified" in c for c in out["capped"])


def test_notion_exact_account_email_resolves_despite_namesake():
    users = [{"object": "user", "type": "person", "id": "u-me", "name": "Parth Singh", "person": {"email": "me@example.com"}},
             {"object": "user", "type": "person", "id": "u-twin", "name": "Parth Singh", "person": {"email": "twin@example.com"}}]
    call, reads = notion_call(users, [page("p1", "u-me", "u-me"), page("p2", "u-twin", "u-twin")])
    out = run_notion(call)
    assert reads == ["p1"] and [d["title"] for d in out["docs"]] == ["Page"]
    assert out["docs"][0]["_attribution"] == "notion account email match"


def test_confluence_v2_identity_ok_opens_only_matching_pages():
    opens = []

    async def call(name, args):
        if name == "confluence.cql_search":
            raise RuntimeError("401 Unauthorized: scope does not match")
        if name == "confluence.get_current_user":
            return {"accountId": "acc-me", "displayName": "Parth Singh"}
        if name == "confluence.get_pages":
            return {"data": {"results": [
                {"id": "1", "title": "Mine", "authorId": "acc-me", "createdAt": "2026-09-01T10:00:00Z",
                 "version": {"createdAt": "2026-09-20T10:00:00Z", "authorId": "acc-me"}, "_links": {"webui": "/pages/1"}},
                {"id": "2", "title": "Theirs", "authorId": "acc-other", "createdAt": "2026-09-01T10:00:00Z",
                 "version": {"createdAt": "2026-09-21T10:00:00Z", "authorId": "acc-other"}, "_links": {"webui": "/pages/2"}}],
                "_links": {"base": "https://x.atlassian.net/wiki"}}}
        if name == "confluence.get_page_by_id":
            opens.append(args["id"])
            return {"id": args["id"], "body": {"storage": {"value": "<p>real work</p>"}},
                    "history": {"createdBy": {"displayName": "Parth Singh"}}, "version": {"by": {"displayName": "Parth Singh"}}}
        if name == "confluence.get_labels_for_page":
            return {"results": []}
        raise RuntimeError(f"unexpected tool {name}")

    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_confluence(call, SINCE, ToolLog(), out, ["Parth Singh"]))
    assert opens == ["1"]  # the unmatched page is never opened
    assert [d["title"] for d in out["docs"]] == ["Mine"] and out["docs"][0]["_mine"] is True
    assert any("1 page(s) not created or edited by your account were not read" in c for c in out["capped"])
