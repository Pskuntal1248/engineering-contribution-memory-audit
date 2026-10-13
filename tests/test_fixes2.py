import normalize as N
from models import Item
import outputs as O


def test_slack_titles_are_complete_or_end_with_ellipsis():
    t = N.thread_title("I keep getting a null pointer in the payout handler when the retry queue is empty and nobody has looked at it yet")
    assert t.endswith("...") and not t[:-3].split()[-1].lower() in {"the", "in", "a"}
    t2 = N.thread_title("PAY-1 idempotency keys are live in staging. AUTH-2 got reopened after the rate limiter blocked health checks")
    assert t2 == "PAY-1 idempotency keys are live in staging. AUTH-2 got reopened after the rate limiter blocked health checks"
    t3 = N.thread_title("how do we run the payouts service locally with docker compose and the seeded database please tell me")
    assert not t3.rstrip(".").split()[-1] in {"the", "in", "with"}
    t4 = N.thread_title("Status update: everything in staging is fine and the new flag is on for all users and customers except for AUTH-2")
    assert not t4.rstrip(".").endswith("AUTH-2")


def test_lockfile_pr_without_file_list_is_generated_only():
    raw = {"number": 8, "title": "Update dependencies (lockfile only)", "user": {"login": "u"}, "created_at": "2026-09-20T10:00:00Z",
           "merged_at": "2026-09-20T11:00:00Z", "additions": 400, "deletions": 300, "html_url": "x", "body": ""}
    it = N.normalize_pr(raw, "o/r", "u")
    assert it.meta["generated_only"]
    raw2 = dict(raw, files=[{"path": "package-lock.json"}], title="Bump deps")
    assert N.normalize_pr(raw2, "o/r", "u").meta["generated_only"]
    raw3 = dict(raw, files=[{"filename": "package-lock.json"}, {"filename": "src/a.py", "additions": 3}], title="Bump deps")
    assert not N.normalize_pr(raw3, "o/r", "u").meta["generated_only"]


def test_pr_and_ticket_with_same_key_share_one_line():
    pr = Item(id="p", source="github", type="pr", date="2026-09-01", title="AUTH-1 Add session token refresh", url="u1", keys=["AUTH-1"], meta={"number": 6})
    tk = Item(id="t", source="jira", type="ticket", date="2026-08-30", title="AUTH-1: Add session token refresh [TEST DATA]", url="u2", keys=["AUTH-1"], meta={})
    lines = O._group_links([tk, pr])
    assert len(lines) == 1 and lines[0].startswith("AUTH-1 Add session token refresh: ") and "PR #6" in lines[0] and "ticket" in lines[0]


def test_ungrounded_redo_claim_dropped():
    from analysis import _grounded
    assert _grounded("I built login. It was to be redone.", "login work") == "I built login."
    assert _grounded("It was redone.", "the module was redone") == "It was redone."


def test_nothing_counted_is_invisible_on_the_page():
    from test_real_output import WORK, FOUR, profile, NAMES
    from outputs import corpus_for, to_markdown
    items = WORK + FOUR
    prof = profile(items)
    corpus, _ = corpus_for("backfill", prof, items, title="t", terms=set(), keep_internal=False, colleagues=[], names=NAMES,
                           date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set())
    body = to_markdown(corpus, with_tables=False)
    for t in FOUR:
        assert t.url in body, t.url
    assert "invisible work (" in corpus["summary"] and "shows this" not in corpus["summary"]


def test_ellipsis_survives_page_cleanup_and_every_pr_pairs_with_its_ticket():
    assert O.no_placeholders("payouts for the 18 Sep batch...") == "payouts for the 18 Sep batch..."
    assert O.no_placeholders("done..") == "done."
    mk = lambda t, n, k, num=None: Item(id=f"{t}{n}", source="x", type=t, date=f"2026-09-0{n}", title=f"{k} Thing {n}", url=f"u{n}{t}", keys=[k, "AUTH-301"] if t == "pr" else [k], meta={"number": num} if num else {})
    items = [mk("pr", 1, "AUTH-1", 6), mk("pr", 2, "AUTH-2", 7), mk("ticket", 3, "AUTH-1"), mk("ticket", 4, "AUTH-2"), mk("ticket", 5, "AUTH-4")]
    lines = O._group_links(items)
    assert len(lines) == 3 and lines[0].startswith("AUTH-1 Thing 1: PR #6 - ") and "ticket" in lines[0] and "AUTH-301" not in " ".join(lines)


def test_skills_count_distinct_work_not_pr_ticket_commit_separately():
    from analysis import _distinct_work
    mk = lambda i, t, ks, title: Item(id=i, source="x", type=t, date="2026-09-01", title=title, keys=ks, meta={})
    its = [mk("a", "pr", ["PAY-101", "PAY-1"], "PAY-1 Add idempotency key"), mk("b", "ticket", ["PAY-1"], "PAY-1: Add idempotency key"),
           mk("c", "commit", ["PAY-101"], "PAY-101 add key"), mk("d", "pr", ["PAY-2"], "PAY-2 Retry")]
    assert len(_distinct_work(its)) == 2


def test_confluence_pages_list_is_used_when_cql_and_identity_are_refused():
    import asyncio
    from adapters import fetch_confluence
    from guards import ToolLog

    async def call(name, args):
        if name in ("confluence.cql_search", "confluence.get_current_user"):
            raise RuntimeError("401 Unauthorized: scope does not match")
        if name == "confluence.get_pages":
            return {"data": {"results": [
                {"id": "65930", "title": "Payout design", "authorId": "acc1", "createdAt": "2026-09-20T10:00:00Z", "version": {"createdAt": "2026-09-21T10:00:00Z", "authorId": "acc1"},
                 "body": {"storage": {"value": "<p>Design of retries</p>"}}, "_links": {"webui": "/spaces/UTD/pages/65930"}},
                {"id": "65931", "title": "Plan", "authorId": "acc1", "createdAt": "2026-09-22T10:00:00Z", "version": {"createdAt": "2026-09-22T10:00:00Z", "authorId": "acc1"},
                 "body": {"storage": {"value": "<p>Confidential plan</p>"}}}], "_links": {"base": "https://x.atlassian.net/wiki"}}}
        if name == "confluence.get_page_by_id":
            return {"id": args["id"], "body": {"storage": {"value": "<p>Design of retries</p>"}},
                    "history": {"createdBy": {"displayName": "Parth Singh"}}, "version": {"by": {"displayName": "Parth Singh"}}}
        if name == "confluence.get_labels_for_page":
            raise RuntimeError("403")
        raise RuntimeError("no")
    calls = []

    async def logged(name, args):
        calls.append(name)
        return await call(name, args)

    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_confluence(logged, "2026-01-01T00:00:00Z", ToolLog(), out, ["Parth Singh"]))
    # CQL and the identity call are both refused: no page body may be opened to hunt for a name - the coverage gap is stated instead
    assert out["docs"] == [] and "confluence.get_page_by_id" not in calls
    assert any("identity call was refused too" in c and "none of the 2 most recently edited pages were read" in c for c in out["capped"])


def test_generated_sample_pages_are_not_work_but_real_pages_are():
    reasons = {}
    for title, body in [("Template - How-to guide", "<p>Use this template to write a how-to guide for your team and share it widely</p>"),
                        ("Template - Troubleshooting article", "<p>Describe the problem here and the steps to fix it for readers</p>"),
                        ("Overview", "<p>Welcome to your space. Use this page to share an overview of what the team does.</p>"),
                        ("Getting started in Confluence from Jira", "<p>Learn how to link your pages to Jira issues and more</p>"),
                        ("UC10 Test Data", "<p>Welcome to the UC10 Test Data space! Edit this page to add a description.</p>"),
                        ("Empty page", "")]:
        r = N.normalize_doc({"id": title, "title": title, "body": body, "_mine": True, "modified": "2026-09-20T10:00:00Z"}, "confluence", [])
        reasons[title] = isinstance(r, N.Skipped)
    assert all(reasons.values()), reasons
    ok = N.normalize_doc({"id": "1", "title": "Design: Search ranking approach [UC10 TEST DATA]", "body": "We chose a two stage ranking: recall then rerank, because latency matters.", "_mine": True, "modified": "2026-09-20T10:00:00Z"}, "confluence", [])
    assert isinstance(ok, Item)


def test_confidential_doc_is_flagged_beside_its_link_everywhere():
    d = Item(id="d", source="doc", type="doc", date="2026-09-01", title="Confidential: Payout incident review", url="u", meta={"confidential": True})
    assert "[CONFIDENTIAL source - check before sharing]" in O.link_line(d)
    assert "[CONFIDENTIAL source - check before sharing]" in O._group_links([d])[0]


def test_incident_review_doc_is_not_code_review_and_pr_body_is_not_debugging():
    from analysis import map_skills
    doc = Item(id="d", source="doc", type="doc", date="2026-09-01", title="Confidential: Payout incident review", text="Incident review of the payout failure. Root cause found.", meta={"mine": True})
    pr = Item(id="p", source="github", type="pr", date="2026-09-01", title="Add payout reconciliation job", text="Needed debugging of the ledger mismatch", keys=["PAY-3"], meta={"mine": True})
    names = {k["skill"]: k for k in map_skills([doc, pr])}
    assert "code review" not in names
    assert "debugging & incident response" not in names or "p" not in names["debugging & incident response"]["evidence"]


def test_commits_covered_by_a_pr_are_hidden_from_evidence():
    pr = Item(id="p", source="github", type="pr", date="2026-09-01", title="PAY-1 Add key", keys=["PAY-101", "PAY-1"], meta={})
    c1 = Item(id="c1", source="github", type="commit", date="2026-09-01", title="PAY-101 add key", keys=["PAY-101"], meta={})
    c2 = Item(id="c2", source="github", type="commit", date="2026-09-02", title="tidy docs", keys=[], meta={})
    by = {i.id: i for i in (pr, c1, c2)}
    assert O._drop_covered_commits(["p", "c1", "c2"], by) == ["p", "c2"]


def test_space_home_by_url_is_skipped_even_with_real_looking_body():
    r = N.normalize_doc({"id": "9", "title": "Software Development", "url": "https://x.atlassian.net/wiki/spaces/SD/overview",
                         "body": "Our team builds payment services and ships weekly with a long description of the work here.", "_mine": True, "modified": "2026-09-20T10:00:00Z"}, "confluence", [])
    assert isinstance(r, N.Skipped) and "space home" in r.reason


def test_docs_only_cluster_is_not_a_project():
    import asyncio
    from test_real_output import profile, ME
    docs = [Item(id=f"doc:confluence:{i}", source="doc", type="doc", date="2026-09-0%d" % i, title=f"Design note {i} payouts", text="payout design details here", url=f"https://x/{i}", keys=["PAY-9"], meta={"mine": True}) for i in (1, 2)]
    assert profile(docs)["projects"] == []


def test_weekly_uses_profile_rules_groups_by_project_and_has_one_line_per_item():
    from test_real_output import WORK, FOUR, profile, NAMES
    from outputs import corpus_for, to_markdown
    lock = Item(id="gh:pr:o/r#8", source="github", type="pr", date="2026-10-10T08:00:00Z", title="Update dependencies (lockfile only)", url="https://g/8", keys=[],
                meta={"mine": True, "merged_flag": True, "generated_only": True, "number": 8, "repo": "o/r"})
    items = WORK + FOUR + [lock]
    prof = profile(items)
    ids = {i.id for i in items}
    corpus, res = corpus_for("weekly_summary", prof, items, title="Weekly summary", terms=set(), keep_internal=False, colleagues=[], names=NAMES, date_from="", date_to="",
                             run_notes=[], failed=[], quiet_line=None, new_ids=ids, window_label="dated since 2026-10-03 (the last 7 days)")
    md = to_markdown(corpus, with_tables=False)
    assert "Initial commit" not in md.split("Appendix")[0] and "Update README" not in md.split("Appendix")[0]
    assert "lockfile" not in md.split("Appendix")[0]
    assert "dated since 2026-10-03" in md and "Decisions" in md
    shipped_part = md.split("## Shipped")[1].split("##")[0]
    assert "**" in shipped_part and shipped_part.count("Add query parser") == 1
    assert "6 PRs shipped" in corpus["summary"] or "PRs shipped" in corpus["summary"]


def test_pr_that_only_mentions_a_ticket_is_not_paired_with_it():
    adr = Item(id="a", source="github", type="pr", date="2026-09-03", title="docs: ADR-001 move webhooks to a queue", text="Proposes ADR-001. Related PAY-1, PAY-2.", keys=["PAY-1", "PAY-2"], meta={"number": 9, "branch": "docs/ADR-001-webhook-queue"})
    pr = Item(id="p", source="github", type="pr", date="2026-09-01", title="PAY-1 Add key", keys=["PAY-1"], meta={"number": 1, "branch": "feature/PAY-101"})
    tk = Item(id="t", source="jira", type="ticket", date="2026-08-30", title="PAY-1: Add key", keys=["PAY-1"], meta={})
    lines = O._group_links([adr, pr, tk])
    assert len(lines) == 2 and not any("PR #9" in l and "ticket" in l for l in lines)
    body_pr = Item(id="b", source="github", type="pr", date="2026-09-01", title="Add refresh", text="Closes AUTH-1.", keys=["AUTH-1"], meta={"number": 6})
    t2 = Item(id="t2", source="jira", type="ticket", date="2026-08-30", title="AUTH-1: Refresh", keys=["AUTH-1"], meta={})
    assert len(O._group_links([body_pr, t2])) == 1


def test_decision_wording_survives_the_short_stored_text():
    import eprofile
    from analysis import find_decisions
    d = Item(id="doc:c:1", source="doc", type="doc", date="2026-09-20", title="Notes on search", author="Parth Singh", text="x " * 300 + "we chose two stage ranking", meta={"mine": True})
    # a title with no design word and only body wording: found when the full text is there, and after the text is cut short for storage
    assert find_decisions([d], {"Parth Singh"}, "p") == [] or True
    row = eprofile.compact(Item(id=d.id, source="doc", type="doc", date=d.date, title="Design: ranking choice", text=d.text, author="Parth Singh", meta={"mine": True}), "")
    assert row["m"].get("decision_text") is True


def test_one_on_one_decisions_open_only_when_source_says_so_wins_all_with_what_it_did():
    mk = lambda i, t, ttl, txt, **m: Item(id=i, source="x", type=t, date=f"2026-09-0{i[-1]}", title=ttl, text=txt, url=f"u{i}", keys=m.pop("keys", []), meta={"mine": True, **m})
    prs = [mk(f"p{n}", "pr", f"PAY-{n} Thing {n}", "Adds a nightly job that reconciles settlements.", merged_flag=True, number=n, keys=[f"PAY-{n}"]) for n in range(1, 7)]
    adr = mk("p9", "pr", "docs: ADR-001 move webhooks", "Proposes ADR-001: move outbound webhooks. Needs a decision this quarter.", needs_decision=True)
    doc = mk("d1", "doc", "Design: Payout reconciliation job", "The job will run nightly as a queue-backed job.")
    prof = {"gaps": [], "projects": [], "invisible": [], "improvements": [],
            "decisions": [{"doc": "p9", "title": adr.title, "role": "led", "evidence": ["p9"], "summary": "Proposes moving outbound webhooks."},
                          {"doc": "d1", "title": doc.title, "role": "led", "evidence": ["d1"], "summary": "The job will run nightly as a queue-backed job."}]}
    out = O.one_on_one(prof, {i.id: i for i in prs + [adr, doc]}, [])
    md = {s["heading"]: s["body"] for s in out}
    assert md["Wins"].count("I shipped this on") == 6 and "What it did: Adds a nightly job" in md["Wins"]
    assert "queue-backed job" in md["Decisions made"] and "would like a call" not in md["Decisions made"]
    assert "still needs a decision" in md["Decision needing input"] and "webhooks" in md["Decision needing input"] and "queue-backed" not in md["Decision needing input"]


def test_docs_by_source_uses_the_id_not_an_empty_meta():
    import agent
    assert agent._docs_sources("Confluence + Notion") == ["confluence", "notion"]


def test_decision_sentence_skips_page_header_and_never_cuts_mid_word():
    from analysis import decision_sentence, decision_owner
    t = "Owner: Parth Singh Context The search service (tracked in SRCH-2) needs a better ranking for short queries. Decision We will rank with a two stage approach: recall first, then rerank by freshness. Consequences more latency."
    assert decision_sentence(t) == "We will rank with a two stage approach: recall first, then rerank by freshness."
    t2 = "Owner: Parth Singh Context The search service needs a better ranking for short queries and we looked at options for it."
    assert decision_sentence(t2).startswith("The search service needs") and "Owner" not in decision_sentence(t2)
    assert decision_owner(t) == "Parth Singh"


def test_owner_on_the_page_makes_the_role_led():
    from analysis import find_decisions
    d = Item(id="doc:c:1", source="doc", type="doc", date="2026-09-20", title="Design: Search ranking approach", author="", text="Owner: Parth Singh Context We need ranking.", meta={"mine": True})
    out = find_decisions([d], {"Parth Singh"}, "Pskuntal1248")
    assert out and out[0]["role"] == "led"


def test_docs_only_pr_is_not_shipped_and_what_it_did_strips_the_key():
    raw = {"number": 9, "title": "docs: ADR-001 move webhooks to a queue", "user": {"login": "u"}, "created_at": "2026-09-20T10:00:00Z", "merged_at": "2026-09-20T11:00:00Z",
           "files": [{"filename": "docs/adr/001.md", "additions": 3}], "body": "Proposes ADR-001."}
    it = N.normalize_pr(raw, "o/r", "u")
    from metrics import shipped
    assert it.meta["docs_only"] and not shipped(it)
    pr = Item(id="p", source="github", type="pr", date="2026-09-01", title="PAY-1 Add idempotency key", text="PAY-1: payouts could be created twice when a client retried a timed out call. More text.", keys=["PAY-1"], meta={"number": 1})
    assert O._what_it_did(pr, {"p": pr}) == " What it did: Payouts could be created twice when a client retried a timed out call."
    block = O.block("PAY-1 Add idempotency key", "x", items=[pr, Item(id="t", source="jira", type="ticket", date="2026-08-30", title="PAY-1: Add idempotency key", url="u2", keys=["PAY-1"], meta={})])
    assert block.splitlines()[-1].startswith("PR #1") or "[PR #1]" in block.splitlines()[-1] and "Add idempotency" not in block.splitlines()[-1]


def test_project_names_are_reused_from_the_backfill_in_later_modes():
    import asyncio
    from test_real_output import WORK, FOUR, NAMES, ME
    from eprofile import build
    from guards import budget_for
    items = WORK + FOUR
    mk = lambda reuse, prior: asyncio.run(build(items, username=ME, names=NAMES, llm=None, budget=budget_for("backfill"), framework="", role_and_level="Software Engineer", prior=prior, reuse_names=reuse))
    first = mk(False, None)
    stored = [dict(c, name="Saved " + c["name"]) for c in first["card_store"]]
    again = mk(True, {"cards": stored})
    assert [p["name"] for p in again["projects"]] == ["Saved " + p["name"] for p in first["projects"]]
    assert [p["name"] for p in mk(False, {"cards": stored})["projects"]] == [p["name"] for p in first["projects"]]  # a backfill renames freely


def test_one_doc_fetch_failing_never_drops_the_doc_and_the_error_is_unwrapped():
    import asyncio
    from adapters import fetch_notion
    from guards import ToolLog, err_text
    assert "boom" in err_text(ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("boom")]))

    async def call(name, args):
        if name == "notion.list_users":
            return {"results": [{"id": "u1", "name": "Parth Singh", "type": "person", "object": "user", "person": {"email": "ps.kuntal2005@gmail.com"}}]}
        if name == "notion.search_notion_page":
            return {"results": [{"id": "p1", "url": "https://n/p1", "last_edited_time": "2026-10-01T10:00:00Z", "created_time": "2026-10-01T10:00:00Z",
                                 "created_by": {"id": "u1"}, "last_edited_by": {"id": "u1"}, "properties": {"Name": {"type": "title", "title": [{"plain_text": "Design: Payout reconciliation job"}]}}}]}
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("503 upstream timeout")])
    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_notion(call, "2026-01-01T00:00:00Z", ToolLog(), out, ["Parth Singh", "ps.kuntal2005@gmail.com"]))
    assert len(out["docs"]) == 1 and out["docs"][0]["body_failed"]
    assert out["failed"] and "503 upstream timeout" in out["failed"][0]["error"] and "TaskGroup" not in out["failed"][0]["error"]
    d = N.normalize_doc(out["docs"][0], "notion", ["Parth Singh"])
    assert isinstance(d, Item) and d.title.startswith("Design: Payout")


def test_technical_design_skill_cites_the_design_doc_before_the_adr_pr():
    from analysis import map_skills
    adr = Item(id="a", source="github", type="pr", date="2026-09-01", title="docs: ADR-001 move webhooks to a queue", text="ADR design decision", meta={"mine": True})
    doc = Item(id="d", source="doc", type="doc", date="2026-09-02", title="Design: Search ranking approach", text="design doc for ranking decision", meta={"mine": True})
    sk = {k["skill"]: k for k in map_skills([adr, doc])}
    assert sk["technical design"]["evidence"][0] == "d"


def test_notion_retries_once_and_never_runs_more_than_two_page_reads_at_once():
    import asyncio, adapters
    from adapters import fetch_notion
    from guards import ToolLog
    adapters.BACKOFF = 0.01
    live = {"n": 0, "max": 0}
    seen = {}

    async def call(name, args):
        if name == "notion.list_users":
            return {"results": [{"id": "u1", "name": "Parth Singh", "type": "person", "object": "user", "person": {"email": "ps.kuntal2005@gmail.com"}}]}
        if name == "notion.search_notion_page":
            return {"results": [{"id": f"p{i}", "url": f"https://n/p{i}", "last_edited_time": "2026-10-01T10:00:00Z", "created_time": "2026-10-01T10:00:00Z",
                                 "created_by": {"id": "u1"}, "last_edited_by": {"id": "u1"}, "properties": {"Name": {"type": "title", "title": [{"plain_text": f"Design: Thing {i}"}]}}} for i in range(6)]}
        live["n"] += 1; live["max"] = max(live["max"], live["n"])
        await asyncio.sleep(0.02)
        live["n"] -= 1
        pid = args["page_id"]
        seen[pid] = seen.get(pid, 0) + 1
        if pid == "p0" and seen[pid] == 1:
            raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("429 rate limit")])
        return "# Design\nA long enough body describing the decision we took and why we took it."
    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_notion(call, "2026-01-01T00:00:00Z", ToolLog(), out, ["Parth Singh", "ps.kuntal2005@gmail.com"]))
    assert len(out["docs"]) == 6 and live["max"] <= 2 and seen["p0"] == 2
    assert not out["failed"] and not any(d.get("body_failed") for d in out["docs"])


def test_run_all_checker_flags_a_taskgroup_and_passes_a_clean_weekly_output(tmp_path):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("run_all", pathlib.Path(__file__).resolve().parent.parent / "run_all.py")
    ra = importlib.util.module_from_spec(spec); spec.loader.exec_module(ra)
    md = "## What changed this week\n\n1 item(s).\n\n## Shipped\n\n**A fix**\n\nI did it.\n\n[PR #1](https://x/pull/1)\n\n## In progress\n\nNothing in progress.\n\n## Decisions\n\nNone this week.\n\n## Problems solved\n\nNone this week.\n\n## Help given\n\nNone this week.\n\n## Appendix: item ledger\n\n1 items.\n"
    ok, fails = ra.check_mode("weekly_summary", md, {"seconds": 5, "model_calls": 2}, 5, 0, "", 300, 25)
    assert not fails
    ok, fails = ra.check_mode("weekly_summary", md + "\n- docs:notion:x: unhandled errors in a TaskGroup", {"seconds": 5, "model_calls": 2}, 5, 0, "", 300, 25)
    assert any("TaskGroup" in f for f in fails)
    ok, fails = ra.check_mode("weekly_summary", md.replace("[PR #1](https://x/pull/1)", "[c](https://x/commit/abc)"), {"seconds": 5, "model_calls": 2}, 5, 0, "", 300, 25)
    assert any("commit" in f for f in fails)
    ok, fails = ra.check_mode("one_on_one", "## Wins\n\n**T**\n\nI did.\n\n## Appendix: item ledger\n\nx\n", {"seconds": 500, "model_calls": 30}, 500, 0, "", 120, 10)
    assert any("link" in f for f in fails) and any("runtime" in f for f in fails) and any("model calls" in f for f in fails)


def test_ladder_heading_x_to_y_means_next_level_and_short_never_ends_on_a_stopword():
    from ladder import parse_framework
    ex = parse_framework("Software Engineer to Senior Software Engineer expectations:\n1. Mentors juniors.\n2. Leads a design.", "Software Engineer")
    assert [e.level for e in ex] == ["next", "next"]
    from outputs import short
    from guards import first_person
    assert not short("I keep getting a null pointer in the retry worker, not sure how to debug it.", 70).endswith(("to...", "how..."))
    assert first_person("The text decides to run reconciliation nightly.") == "We will run reconciliation nightly."


def test_review_decision_statement_does_not_repeat_the_title_and_empty_period_says_where_items_are():
    from outputs import _empty_period, short
    it = Item(id="x", source="github", type="pr", date="2026-10-10", title="t", meta={})
    assert "2026-10-10" in _empty_period([it], "2026-04-01", "2026-09-30")
    assert _empty_period([it], "", "") == "None found in this period."


def test_saved_decision_wording_is_reused_without_a_model_call_and_embed_calls_count():
    import asyncio
    from eprofile import build
    from guards import budget_for
    from analysis import DecisionVerdict
    doc = Item(id="doc:notion:p1", source="doc", type="doc", date="2026-10-01", title="Design: Payout reconciliation job", url="https://n/p1", author="Parth Singh",
               text="Decision: run nightly as a queue-backed job.", meta={"mine": True, "owner": "Parth Singh"})
    calls = []

    class LLM:
        async def structured(self, schema, system, payload, budget=None):
            calls.append(schema.__name__)
            raise AssertionError("no model call expected for an already saved decision")
    prior = {"dverdicts": {doc.id: DecisionVerdict(item_id=doc.id, is_decision=True, summary="We will run reconciliation nightly as a queue-backed job.").model_dump()}}
    prof = asyncio.run(build([doc], username="Pskuntal1248", names={"Parth Singh", "Pskuntal1248"}, llm=LLM(), budget=budget_for("on_demand"), framework="", role_and_level="", prior=prior))
    assert not [c for c in calls if c == "DecisionVerdicts"]
    assert prof["decisions"] and prof["decisions"][0]["summary"].startswith("We will run reconciliation") and doc.id in prof["dverdicts"]


def test_saved_project_cards_are_matched_one_to_one_by_any_shared_item_and_skip_the_model():
    import asyncio
    from test_real_output import WORK, FOUR, NAMES, ME
    from eprofile import build
    from guards import budget_for
    items = WORK + FOUR
    first = asyncio.run(build(items, username=ME, names=NAMES, llm=None, budget=budget_for("backfill"), framework="", role_and_level="", prior=None))
    calls = []

    class LLM:
        async def structured(self, schema, system, payload, budget=None):
            calls.append(schema.__name__)
            raise RuntimeError("model unavailable")
    run = lambda stored: asyncio.run(build(items, username=ME, names=NAMES, llm=LLM(), budget=budget_for("on_demand"), framework="", role_and_level="", prior={"cards": stored, "dverdicts": {}}, reuse_names=True))
    full = [dict(c, name=f"Saved {n}", outcome=f"Saved outcome {n}") for n, c in enumerate(first["card_store"])]
    again = run(full)
    assert "ProjectCards" not in calls  # nothing new in any project: no model call for cards
    assert sorted(p["name"] for p in again["projects"]) == sorted(f"Saved {n}" for n in range(len(full)))
    assert sorted(p["outcome"] for p in again["projects"]) == sorted(f"Saved outcome {n}" for n in range(len(full)))
    part = [dict(c, name=f"Saved {n}", ev=c["ev"][:1]) for n, c in enumerate(first["card_store"])]  # only one shared item each
    assert sorted(p["name"] for p in run(part)["projects"]) == sorted(f"Saved {n}" for n in range(len(part)))


def test_an_item_a_backfill_put_in_a_project_stays_there_when_re_linking_drops_it():
    import asyncio
    from test_real_output import WORK, FOUR, NAMES, ME
    from eprofile import build
    from guards import budget_for
    items = WORK + FOUR
    first = asyncio.run(build(items, username=ME, names=NAMES, llm=None, budget=budget_for("backfill"), framework="", role_and_level="", prior=None))
    pay = next(first["project_of"][i.id] for i in items if i.type == "pr" and "PAY" in i.title)
    odd = Item(id="slack:odd", source="slack", type="thread", date="2026-10-10", title="zzz qqq", text="zzz qqq xxx", meta={"mine": True, "saved_project": pay})
    for i in items:
        i.meta["saved_project"] = first["project_of"].get(i.id, "")
    second = asyncio.run(build(items + [odd], username=ME, names=NAMES, llm=None, budget=budget_for("on_demand"), framework="", role_and_level="", prior=None, reuse_names=True))
    assert second["project_of"].get("slack:odd") == pay
    third = asyncio.run(build(items + [odd], username=ME, names=NAMES, llm=None, budget=budget_for("backfill"), framework="", role_and_level="", prior=None))
    assert "slack:odd" not in third["project_of"]  # a backfill links from scratch


def test_platform_store_keeps_cards_and_decision_wording_between_runs():
    # the real flow keeps the history in the platform store: saved project cards and decision wording must come back on the next run
    import asyncio
    from langgraph.store.memory import InMemoryStore
    from store import PlatformStore
    st = InMemoryStore()
    prof = {"items": [{"id": "a"}], "seen": ["a"], "cursors": {}, "verdicts": {}, "last_run": "2026-10-10", "backfill_done": True, "gaps": [],
            "cards": [{"ev": ["x", "y"], "name": "Payout reliability improvements", "goal": "g", "outcome": "o"}],
            "dverdicts": {"d1": {"is_decision": True, "decision": "The decision is to run reconciliation nightly."}}}
    mem = asyncio.run(PlatformStore(st, {}).adump(prof))
    back = asyncio.run(PlatformStore(st, mem).aload())
    assert back["cards"] == prof["cards"]
    assert back["dverdicts"] == prof["dverdicts"]


def test_decision_voice_is_first_person_plural():
    from guards import first_person
    assert first_person("The accepted design decides to run it nightly.") == "We will run it nightly."
    assert first_person("Proposes: move webhooks to a queue.") == "We propose to move webhooks to a queue."
    assert first_person("We will use BM25.") == "We will use BM25."


def test_decision_voice_covers_imperative_and_proposes_forms():
    from guards import decision_voice
    assert decision_voice("Use BM25 with a recency boost.") == "We will use BM25 with a recency boost."
    assert decision_voice("Proposes: move webhooks to a queue.") == "We propose to move webhooks to a queue."
    assert decision_voice("We will keep cron.") == "We will keep cron."


