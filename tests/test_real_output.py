"""The shape of the user's real run (synthetic repo PAY/SRCH/AUTH, 4 Slack topics, init commits), pinned as tests."""
import asyncio

from eprofile import build
from guards import budget_for
from models import Item
from outputs import corpus_for
from pipeline import merge_threads

ME = "Pskuntal1248"
NAMES = {ME, "Parth Singh"}
R = "Pskuntal1248/uc10-synthetic-test-data"


def pr(n, key, title, body="", merged=True, ts="2026-10-10T08:00:00Z"):
    return Item(id=f"gh:pr:{R}#{n}", source="github", type="pr", date=ts, title=f"{key} {title}", url=f"https://github.com/{R}/pull/{n}", text=body, author=ME, keys=[key],
                meta={"mine": True, "repo": R, "number": n, "merged_flag": merged, "state": "closed"})


def ticket(key, title, ts="2026-10-10T07:00:00Z"):
    return Item(id=f"jira:{key}", source="jira", type="ticket", date=ts, title=f"{key} {title}", url=f"https://x.atlassian.net/browse/{key}", text=title, author="Parth Singh", keys=[key],
                meta={"mine": True, "status": "Done"})


def commit(sha, title):
    return Item(id=f"gh:commit:{R}@{sha}", source="github", type="commit", date="2026-10-10T06:00:00Z", title=title, url=f"https://github.com/{R}/commit/{sha}", author=ME,
                meta={"mine": True, "repo": R})


def thread(i, minute, text, keys=()):
    return Item(id=f"slack:G1:17900{i}.000100", source="slack", type="thread", date=f"2026-10-10T09:{minute:02d}:00Z", title="#uc10-test thread", url=f"https://s/p{i}",
                text=f"Parth Singh: {text}\nParth Singh: note{i}x{i}", author="Parth Singh", keys=list(keys),
                meta={"channel": "uc10-test", "channel_id": "G1", "messages": 2, "my_messages": 2, "participants": ["Parth Singh"], "mine": True})


FOUR = [
    thread(1, 0, "Settlement totals mismatch for the 18 Sep batch. Traced it: the batch job used UTC while the cutoff is IST, so I fixed the window config"),
    thread(2, 4, "Walked a new joiner through the null pointer in the retry worker, mentored them on reading the stack and pairing on the fix"),
    thread(3, 7, "How do I run the payouts service locally? Explained: start the compose stack, set the env file, then use the replay script here"),
    thread(4, 9, "Status: PAY-1 idempotency is shipped and AUTH-2 was reopened after the token refresh bug, tracking both", keys=("PAY-1", "AUTH-2")),
]
WORK = [
    ticket("PAY-1", "Add idempotency key to payouts"), ticket("PAY-2", "Retry failed payouts with backoff"), ticket("PAY-3", "Add payout reconciliation job"),
    ticket("SRCH-1", "Add query parser"), ticket("SRCH-2", "Add result ranking"), ticket("AUTH-1", "Add session token refresh"), ticket("AUTH-2", "Fix token refresh race"),
    pr(1, "PAY-1", "Add idempotency key to payouts"), pr(2, "PAY-2", "Retry failed payouts with backoff"),
    pr(4, "PAY-3", "Add payout reconciliation job", body="Reconciles settlements nightly. Needed debugging of the ledger mismatch and a component for the report."),
    pr(3, "SRCH-1", "Add query parser"), pr(5, "SRCH-2", "Add result ranking"), pr(6, "AUTH-1", "Add session token refresh", body="Adds a refresh component for the session client."),
    commit("445b034", "Initial commit"), commit("cbe3ec0", "Update README.md"),
]


def profile(items):
    return asyncio.run(build(items, username=ME, names=NAMES, llm=None, budget=budget_for("backfill"), framework="", role_and_level="Software Engineer", prior=None))


def test_four_topics_posted_minutes_apart_stay_four_items():
    out = merge_threads([Item(**t.__dict__) for t in FOUR])
    assert len(out) == 4


def test_invisible_work_keeps_the_three_unticketed_threads_and_not_the_status_thread():
    prof = profile(WORK + FOUR)
    labels = sorted(v["label"] for v in prof["invisible"])
    assert prof["invisible"] and len(prof["invisible"]) == 3, labels
    assert {"problem_solved", "mentoring", "help_given"} == set(labels)
    assert all("AUTH-2" not in v["summary"] for v in prof["invisible"])


def test_projects_follow_pay_srch_auth_with_readable_names_and_no_init_project():
    prof = profile(WORK + FOUR)
    names = [p["name"] for p in prof["projects"]]
    assert len(names) == 3, names
    assert sorted(n.rsplit("(", 1)[1].rstrip(")") for n in names) == ["AUTH", "PAY", "SRCH"]
    assert all(n[0].isupper() and "Srch" not in n and "Pay " not in n for n in names)
    assert sorted(names) == ["Payouts (PAY)", "Query parser (SRCH)", "Session token refresh (AUTH)"], names
    ev = {p["name"]: set(p["evidence"]) for p in prof["projects"]}
    pay = next(v for k, v in ev.items() if k.endswith("(PAY)"))
    assert f"gh:pr:{R}#1" in pay and f"gh:pr:{R}#6" not in pay and "jira:AUTH-1" not in pay
    assert not any("commit" in e for p in prof["projects"] for e in p["evidence"])  # init/README commits are not a project


def test_skills_need_real_evidence():
    prof = profile(WORK + FOUR)
    by = {s["skill"]: s["evidence"] for s in prof["skills"]}
    assert not any("pull/4" in e or e.endswith("#4") for e in by.get("debugging & incident response", []) if "gh:pr" in e)
    assert f"gh:pr:{R}#6" not in by.get("frontend", []) + by.get("accessibility", [])


def test_profile_page_has_all_seven_sections_even_when_empty_and_the_ledger_is_last():
    prof = profile([ticket("PAY-1", "Add idempotency key to payouts")])
    corpus, _ = corpus_for("backfill", prof, [ticket("PAY-1", "Add idempotency key to payouts")], title="t", terms=set(), keep_internal=False, colleagues=[], names=NAMES,
                           date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set())
    heads = [s["heading"] for s in corpus["sections"]]
    for h in ("Projects", "Problems solved", "Decisions", "Invisible work", "Skills", "Improvement areas", "Evidence gaps"):
        assert h in heads
    assert not any("Key numbers" in h for h in heads) and heads[-1].startswith("Appendix: item ledger")
    tables = [s for s in corpus["sections"] if s.get("table")]
    assert len(tables) == 1 and tables[0] is corpus["sections"][-1]  # the CSV export reads the first (only) table
    empty = next(s for s in corpus["sections"] if s["heading"] == "Invisible work")
    assert empty["body"].startswith("None found")


def test_placeholder_phrases_and_double_periods_never_show():
    from outputs import no_placeholders
    assert no_placeholders("An initial commit and a README update are recorded; further outcome not stated..") == "An initial commit and a README update are recorded."


def test_slack_items_get_a_real_title_and_link_and_the_page_has_blocks_with_links_on_their_own_line():
    import normalize as N
    msgs = [{"ts": "1791641890.36", "user": "U1", "text": "Settlement totals mismatch for the 18 Sep batch, looks like a timezone problem"}]
    it = N.normalize_thread("uc10-test", "G1", msgs, {"U1"}, "https://the-glider.slack.com")
    assert it.title == "Settlement totals mismatch for the 18 Sep batch, looks like a timezone problem" and it.url == "https://the-glider.slack.com/archives/G1/p179164189036"
    assert N.normalize_thread("uc10-test", "G1", msgs, {"U1"}).url.startswith("https://slack.com/app_redirect?channel=G1&message_ts=")
    items = merge_threads(WORK + [Item(**t.__dict__) for t in FOUR])
    corpus, _ = corpus_for("backfill", profile(items), items, title="t", terms=set(), keep_internal=False, colleagues=[], names=NAMES,
                           date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set())
    from outputs import to_markdown
    md = to_markdown(corpus, with_tables=False)
    assert "\n\n**" in md and "\nSource: " in md and "the user" not in md.lower()
    solved = next(s for s in corpus["sections"] if s["heading"] == "Problems solved")["body"]
    inv = next(s for s in corpus["sections"] if s["heading"] == "Invisible work")["body"]
    assert "listed once, under Problems solved" in inv and "Settlement totals" not in inv  # the same thread is not repeated verbatim
    assert "Settlement totals" in solved


def test_notion_profile_and_untitled_pages_are_not_counted_as_docs():
    import normalize as N
    assert isinstance(N.normalize_doc({"id": "1", "title": "Parth Singh", "body": ""}, "notion", ["Parth Singh"]), N.Skipped)
    assert isinstance(N.normalize_doc({"id": "2", "title": "", "body": "x"}, "notion", []), N.Skipped)
    assert isinstance(N.normalize_doc({"id": "3", "title": "Design: ranking", "body": "We chose a two stage ranking approach", "modified": "2026-09-01"}, "notion", ["Parth Singh"]), Item)


def test_error_text_is_clean_and_pr_and_ticket_share_one_evidence_line():
    from readers import explain
    from outputs import block
    msg = explain("docs:confluence", 'tool said: {"code":401,"message":"Unauthorized; token has expired for this call","details":"x"}')
    assert "Unauthorized; token has expired for this call" in msg and not msg.rstrip(").").endswith("s")
    pr_, tk = pr(6, "AUTH-1", "Add session token refresh"), ticket("AUTH-1", "Add session token refresh")
    out = block("Project", "One sentence.", items=[pr_, tk])
    assert out.count("Add session token refresh") == 1 and "PR #6 - https://" in out and "ticket - https://" in out
    assert out.split("\n\n")[0] == "**Project**" and out.split("\n\n")[1] == "One sentence."


def test_messages_folded_into_a_conversation_are_listed_as_not_counted():
    from pipeline import Config, ingest
    msgs = lambda ts, t: {"ts": ts, "user": "U1", "text": t}
    raw = {"slack": {"channels": [{"id": "G1", "name": "uc10-test"}], "my_user_ids": ["U1"], "threads": [
        {"channel": "uc10-test", "channel_id": "G1", "messages": [msgs("1791641890.36", "the payout batch window timezone is wrong")]},
        {"channel": "uc10-test", "channel_id": "G1", "messages": [msgs("1791641922.86", "fixed the payout batch window timezone")]}]}}
    ing = ingest(raw, Config(username="u", repos=[], slack_channels=["uc10-test"]))
    assert len([i for i in ing.items if i.type == "thread"]) == 1 and any("folded into the same Slack conversation" in s.reason for s in ing.skipped)


def test_review_material_is_situation_action_result_per_project_with_links():
    items = merge_threads(WORK + [Item(**t.__dict__) for t in FOUR])
    corpus, _ = corpus_for("review_material", profile(items), items, title="t", terms=set(), keep_internal=False, colleagues=[], names=NAMES,
                           date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set())
    body = corpus["sections"][0]["body"]
    assert body.count("- Situation:") == 3 and body.count("- Action:") == 3 and body.count("- Result:") == 3
    assert body.count("Evidence:") >= 3 and " - https://" in body and "1 PRs" not in body


def test_confluence_falls_back_to_the_pages_list_when_the_cql_search_is_refused_for_scope():
    import asyncio
    from readers import fetch_confluence, explain
    from guards import ToolLog
    calls = []

    async def call(tool, args):
        calls.append(tool)
        if tool == "confluence.cql_search":
            raise RuntimeError('{"code":401,"message":"Unauthorized; scope does not match"}')
        if tool == "confluence.get_current_user":
            return {"data": {"account_id": "acc-me", "display_name": "Parth Singh"}}
        if tool == "confluence.get_pages":
            return {"data": {"results": [
                {"id": "11", "title": "Design: ranking", "authorId": "acc-me", "createdAt": "2026-09-20T10:00:00Z", "version": {"authorId": "acc-me", "createdAt": "2026-09-21T10:00:00Z"},
                 "body": {"storage": {"value": "<p>Ranking design for SRCH-2</p>"}}, "_links": {"webui": "/spaces/UTD/pages/11"}},
                {"id": "12", "title": "Someone else", "authorId": "acc-x", "createdAt": "2026-09-20T10:00:00Z", "version": {"authorId": "acc-x"}, "body": {"storage": {"value": "x"}}}],
                "_links": {"base": "https://x.atlassian.net/wiki"}}}
        if tool == "confluence.get_labels_for_page":
            return {"data": {"results": [{"name": "confidential"}]}}
        raise AssertionError(tool)
    out = {"docs": [], "failed": [], "capped": []}
    asyncio.run(fetch_confluence(call, "2026-04-01", ToolLog(), out))
    assert [d["id"] for d in out["docs"]] == ["11"] and out["docs"][0]["labels"] == ["confidential"] and not out["failed"]
    assert out["docs"][0]["url"] == "https://x.atlassian.net/wiki/spaces/UTD/pages/11"
    assert "connected but its permission scope does not match; reconnect with: gravity connect confluence" in explain("docs:confluence", '{"code":401,"message":"Unauthorized; scope does not match"}')


def test_lockfile_only_pr_trivial_commits_unknown_keys_and_single_dates():
    from readers import rows as _r  # noqa: F401
    import normalize as N
    from metrics import shipped
    from outputs import _drop_unknown_keys, _span
    import ledger
    raw = {"number": 8, "title": "Update dependencies", "user": {"login": ME}, "created_at": "2026-10-10T08:00:00Z", "merged_at": "2026-10-10T09:00:00Z",
           "files": [{"filename": "poetry.lock", "additions": 4000, "deletions": 3}]}
    it = N.normalize_pr(raw, R, ME)
    assert not shipped(it) and ledger.counted_label(it).startswith("generated or lockfile")
    for t in ("Initial commit", "Update README.md", "Update print statement from 'Hello' to 'Goodbye'"):
        assert ledger.counted_label(commit("abc", t)).startswith("listed, not counted")
    assert _drop_unknown_keys("Move webhooks to a queue. Related PAY-1, PAY-2, PAY-110.", {"PAY-1", "PAY-2"}) == "Move webhooks to a queue. Related PAY-1, PAY-2."
    assert _span({"first": "2026-10-10", "last": "2026-10-10"}) == "2026-10-10"


def test_skills_count_distinct_work_not_pr_plus_ticket_plus_commit():
    from analysis import map_skills
    items = [pr(1, "PAY-1", "Add idempotency key to payouts"), ticket("PAY-1", "Add idempotency key to payouts"), commit("c1", "PAY-1 idempotency key for payouts")]
    items[2].keys = ["PAY-1"]
    sk = {k["skill"]: k for k in map_skills(items)}
    assert sk["payments & idempotency"]["count"] == 1
