"""Jira, docs, Slack-search, embeddings and the platform store. Fake gateway shapes built from the tool catalog's argument names;
the response shapes are assumptions until a real run. No real tools or models."""
import asyncio
from datetime import datetime, timezone

import agent
import adapters
from guards import ToolLog, parse_channels
from sources import GatewaySource
from store import PlatformStore
from langgraph.store.memory import InMemoryStore

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
ME = "Pskuntal1248"
SINCE = "2026-04-10T00:00:00Z"


def adf(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def issue(key, summary, created="2026-08-01T10:00:00.000+0000"):
    return {"key": key, "fields": {"summary": summary, "created": created, "updated": created, "status": {"name": "Done"},
                                   "assignee": {"displayName": "Parth Singh"}, "reporter": {"displayName": "Parth Singh"}, "description": adf("Queue the webhooks " + key)}}


class Gateway:
    def __init__(self):
        self.calls = []
        self.live = self.peak = 0

    async def __call__(self, name, args):
        self.calls.append((name, args))
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(0.005)
        self.live -= 1
        if name == "jira.get_all_projects":
            return {"values": [{"key": "PAY"}]}
        if name == "jira.search_issues":
            if args.get("next_page_token") == "p2":
                return {"issues": [issue("PAY-3", "third")], "isLast": True}
            return {"issues": [issue("PAY-1", "first"), issue("PAY-2", "second")], "nextPageToken": "p2"}
        if name == "jira.get_issue":
            k = args["issue_id_or_key"]
            hist = []
            if k == "PAY-1":
                hist = [{"created": "2026-08-02T00:00:00.000+0000", "items": [{"field": "status", "fromString": "Done", "toString": "In Progress"}]},
                        {"created": "2026-08-03T00:00:00.000+0000", "items": [{"field": "Key", "fromString": "OLD-9", "toString": "PAY-1"}]}]
            return {"key": k, "fields": {"comment": {"total": 3 if k == "PAY-2" else 0, "comments": [{"body": adf("c1")}] if k == "PAY-2" else []}}, "changelog": {"histories": hist}}
        if name == "jira.list_issue_comments":
            return {"comments": [{"body": adf("c2")}, {"body": adf("c3")}]}
        if name == "confluence.cql_search":
            return {"results": [{"content": {"id": "77", "title": "ADR-001 queue", "history": {"createdBy": {"displayName": "Parth Singh"}, "createdDate": "2026-08-05T00:00:00Z"},
                                             "version": {"by": {"displayName": "Parth Singh"}, "when": "2026-08-06T00:00:00Z"},
                                             "metadata": {"labels": {"results": [{"name": "confidential"}]}}}, "lastModified": "2026-08-06T00:00:00Z", "url": "/wiki/x/77"}]}
        if name == "confluence.get_page_by_id":
            return {"id": "77", "body": {"storage": {"value": "<p>We decided to move webhooks to a queue</p>"}}}
        if name == "notion.list_users":
            return {"data": {"response_data": {"results": [{"object": "user", "id": "u-me", "type": "person", "name": "Parth Singh", "person": {"email": "ps.kuntal2005@gmail.com"}},
                                                           {"object": "user", "id": "u-other", "type": "person", "name": "Other Person"},
                                                           {"object": "user", "id": "u-bot", "type": "bot", "name": "Integration"}]}}}
        if name == "notion.search_notion_page":
            return {"data": {"response_data": {"results": [{"id": "n1", "created_by": {"id": "u-me"}, "last_edited_by": {"id": "u-me"}, "last_edited_time": "2026-09-01T00:00:00Z", "created_time": "2026-09-01T00:00:00Z", "url": "https://notion.so/n1",
                                 "properties": {"Name": {"type": "title", "title": [{"plain_text": "Design notes"}]}}},
                                {"id": "n2", "created_by": {"id": "someone"}, "last_edited_by": {"id": "someone"}, "last_edited_time": "2026-09-02T00:00:00Z", "properties": {}}]}}}
        if name == "notion.get_page_markdown":
            return {"markdown": "# Design notes\nwe chose a queue"}
        if name == "drive.get_about":
            return {"user": {"displayName": "Parth Singh", "emailAddress": "ps.kuntal2005@gmail.com", "permissionId": "pid-me"}}
        if name == "drive.find_file":
            return {"files": [{"id": "d1", "name": "Spec", "modifiedTime": "2026-09-03T00:00:00Z", "owners": [{"displayName": "Parth Singh", "emailAddress": "ps.kuntal2005@gmail.com"}],
                               "lastModifyingUser": {"displayName": "Parth Singh", "emailAddress": "ps.kuntal2005@gmail.com"}, "webViewLink": "https://docs.google.com/d1"}]}
        if name == "drive.get_document":
            return {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "spec body with the details of the retry design"}}]}}]}}
        if name == "slack.list_channels":
            return {"channels": [{"id": "C1", "name": "payments-eng"}, {"id": "C2", "name": "random"}, {"id": "D1", "name": "dm-x", "is_im": True}]}
        if name == "slack.search_messages":
            assert "in:#payments-eng" in args["query"] and "from:me" in args["query"]
            return {"messages": {"matches": [{"channel": {"id": "C1"}, "ts": "1790000000.000100", "user": "U1", "text": "fixed it"},
                                              {"channel": {"id": "C2"}, "ts": "1790000100.000100", "user": "U1", "text": "other channel, must be dropped"}]}}
        if name == "slack.fetch_message_thread_from_a_conversation":
            assert args["channel"] == "C1"
            return {"messages": [{"ts": "1790000000.000100", "user": "U2", "text": "settlement mismatch in the batch job again"},
                                 {"ts": "1790000050.000100", "user": "U1", "text": "found it: a timezone bug in the batch job, patching it now and adding a test"}]}
        raise RuntimeError("unexpected " + name)


HAS = {"jira.search_issues", "jira.get_issue", "jira.list_issue_comments", "jira.get_all_projects", "confluence.cql_search", "confluence.get_page_by_id", "notion.list_users", "notion.search_notion_page",
       "notion.get_page_markdown", "drive.find_file", "drive.get_document", "slack.list_channels", "slack.read_history", "slack.search_messages",
       "slack.fetch_message_thread_from_a_conversation"}


def fetch(gw, docs=("confluence", "notion", "google drive")):
    src = GatewaySource(gw, ME, [], parse_channels("#payments-eng"), False, ["PAY"], HAS, list(docs), ["Parth Singh", "ps.kuntal2005@gmail.com"])
    return asyncio.run(src.fetch(SINCE, None, ToolLog()))


def test_jira_pages_history_comments_and_aliases():
    gw = Gateway()
    out = fetch(gw, docs=())
    keys = [t["key"] for t in out["jira"]]
    assert keys == ["PAY-1", "PAY-2", "PAY-3"]
    q = next(a for n, a in gw.calls if n == "jira.search_issues")
    assert 'project in (PAY)' in q["jql"] and "currentUser()" in q["jql"]
    assert [c for c in gw.calls if c[0] == "jira.get_issue"][0][1]["expand"] == "changelog"
    assert len(next(t for t in out["jira"] if t["key"] == "PAY-2")["fields"]["comment"]["comments"]) == 3  # paged the rest
    assert gw.peak <= 6


def test_ticket_normalises_adf_reopens_and_moved_key():
    from normalize import normalize_ticket
    out = fetch(Gateway(), docs=())
    t = normalize_ticket(out["jira"][0], "https://x.atlassian.net", ["Parth Singh"])
    assert t.meta["reopen_count"] == 1 and "OLD-9" in t.meta["aliases"] and t.meta["mine"] is True
    assert "Queue the webhooks" in t.text


def test_docs_from_all_three_sources_with_confidential_mark():
    from normalize import normalize_doc
    out = fetch(Gateway())
    by = {d["_source"]: d for d in out["docs"]}
    assert set(by) == {"confluence", "notion", "drive"} and len(out["docs"]) == 3  # n2 (someone else's page) is not included
    c = normalize_doc(by["confluence"], "confluence", ["Parth Singh"])
    assert c.meta["confidential"] is True and "move webhooks to a queue" in c.text and "<p>" not in c.text


def test_slack_search_stays_in_chosen_channels_and_reads_no_dms():
    gw = Gateway()
    log = ToolLog()
    out = asyncio.run(GatewaySource(gw, ME, [], parse_channels("#payments-eng"), False, [], HAS).fetch(SINCE, None, log))
    assert len(out["slack"]["threads"]) == 1 and out["slack"]["threads"][0]["channel_id"] == "C1"
    assert out["slack"]["my_user_ids"] == ["U1"]
    assert not log.dm_reads()
    assert not any(n == "slack.read_history" for n, _ in gw.calls)


def test_full_run_uses_platform_store_and_keeps_memory_small():
    gw = Gateway()
    store = InMemoryStore()
    deps = agent.Deps(lambda cfg: GatewaySource(gw, cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, HAS, cfg.docs_sources, cfg.name_hints),
                      llm=None, store_factory=lambda mem: PlatformStore(store, mem), now=lambda: NOW)
    inp = {"github_username": ME, "repos_or_org": "o/r", "jira_projects": "PAY", "slack_channels": "#payments-eng", "docs_source": "All three",
           "role_and_level": "Software Engineer", "display_name": "Parth Singh, ps.kuntal2005@gmail.com"}
    g = agent.make_graph(deps)
    # no GitHub tools in the fake: that source is named as failed, the rest still works
    out = asyncio.run(g.ainvoke({"inputs": inp, "memory": {}}))
    mem = out["memory"]
    assert set(mem) == {"marker"} and mem["marker"]["backfill_done"] is True
    assert len(str(mem)) < 2000
    keys = {i.key for i in asyncio.run(store.asearch(("uc10", "profile"), limit=100))}
    assert {"items-0000", "seen", "verdicts"} <= keys
    r = out["result"]
    assert r["stats"]["tickets_touched"] == 3 and r["stats"]["docs"] == 3 and r["stats"]["threads"] == 1
    # second run reads the history back from the store and runs as weekly
    out2 = asyncio.run(g.ainvoke({"inputs": inp, "memory": mem}))
    assert out2["result"]["mode"] == "weekly" and out2["result"]["ledger_rows"] == 0  # weekly lists only new rows
    # the store lost its data but memory says there was a backfill: say so and backfill again
    out3 = asyncio.run(agent.make_graph(agent.Deps(deps.source_factory, store_factory=lambda mem: PlatformStore(InMemoryStore(), mem), now=lambda: NOW)).ainvoke({"inputs": inp, "memory": mem}))
    assert out3["result"]["mode"] == "backfill"


class FakeEmbedder:
    calls = 1

    def __init__(self, fail=False):
        self.fail = fail

    async def embed(self, items):
        if self.fail:
            return {}, ["embeddings unavailable, grouped by keywords instead: boom"]
        # two topics: items mentioning 'queue' point one way, everything else the other
        return {i.id: ([1.0, 0.0] if "queue" in (i.title + i.text).lower() else [0.0, 1.0]) for i in items}, []


def test_embeddings_join_the_link_and_failure_falls_back():
    from linking import blend, tfidf_vectors
    from models import Item
    a = Item(id="a", source="x", type="pr", date="2026-08-01T00:00:00Z", title="alpha", text="zzz", meta={"repo": "o/r", "number": 1})
    b = Item(id="b", source="x", type="pr", date="2026-08-01T00:00:00Z", title="beta", text="yyy", meta={"repo": "o/r", "number": 2})
    sparse = tfidf_vectors([a, b])
    mixed = blend(sparse, {"a": [1.0, 0.0], "b": [1.0, 0.0]})
    from linking import cosine
    assert cosine(sparse["a"], sparse["b"]) == 0 and cosine(mixed["a"], mixed["b"]) > 0.4  # same embedding topic links them
    assert blend(sparse, None) is sparse
    from eprofile import build
    from guards import budget_for
    for fail in (False, True):
        prof = asyncio.run(build([a, b], username=ME, names=set(), llm=None, budget=budget_for("backfill"), framework="", role_and_level="", prior=None, embedder=FakeEmbedder(fail)))
        assert any("embeddings" in n for n in prof["notes"])


def test_gateway_embedder_centres_and_fails_soft(monkeypatch):
    import embed
    from models import Item

    class Boom:
        async def aembed_documents(self, texts):
            raise RuntimeError("endpoint down")

    e = embed.GatewayEmbedder("http://x", "t")
    e._e = Boom()
    v, notes = asyncio.run(e.embed([Item(id="a", source="x", type="pr", date="d", title="t")]))
    assert v == {} and "keywords" in notes[0]
    c = embed.center({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    assert abs(embed.dense_cosine(c["a"], c["b"]) + 1.0) < 1e-9


def test_notion_identity_by_name_solo_workspace_and_wrapped_shapes():
    out = fetch(Gateway(), docs=("notion",))
    assert [d["id"] for d in out["docs"]] == ["n1"] and out["docs"][0]["author"] == "Parth Singh"  # two people: only the user's own page, listed under the user's name
    assert not out["failed"]

    class Solo(Gateway):
        async def __call__(self, name, args):
            if name == "notion.list_users":
                return {"results": [{"object": "user", "id": "u-only", "type": "person", "name": "Someone Else"}]}
            return await super().__call__(name, args)

    solo_gw = Solo()
    solo = fetch(solo_gw, docs=("notion",))
    # one person in the workspace is not proof of identity: nothing is read or credited
    assert solo["docs"] == [] and not any("single-person" in c for c in solo["capped"])
    assert any("your Notion user could not be verified" in c for c in solo["capped"])
    assert not any(n == "notion.get_page_markdown" for n, _ in solo_gw.calls)

    class Crowd(Gateway):
        async def __call__(self, name, args):
            if name == "notion.list_users":
                return {"results": [{"object": "user", "id": "a", "type": "person", "name": "A B"}, {"object": "user", "id": "b", "type": "person", "name": "C D"}]}
            return await super().__call__(name, args)

    crowd_gw = Crowd()
    crowd = fetch(crowd_gw, docs=("notion",))
    assert crowd["docs"] == [] and "could not tell which of 2" in crowd["failed"][0]["error"]
    assert not any(n == "notion.get_page_markdown" for n, _ in crowd_gw.calls)


def test_rows_reads_wrapped_envelopes():
    assert adapters.rows({"data": {"response_data": {"issues": [{"key": "A-1"}]}}}, "issues") == [{"key": "A-1"}]
    assert adapters.find_key({"data": {"x": {"next_page_token": "t"}}}, "next_page_token") == "t"


def test_adr_pr_counts_even_if_the_model_says_no_and_threads_alone_are_not_projects():
    from analysis import DecisionVerdict, find_decisions
    from models import Item
    adr = Item(id="gh:pr:o/r#9", source="github", type="pr", date="2026-09-04T00:00:00Z", title="docs: ADR-001 move webhooks to a queue", text="Proposes ADR-001.", author=ME,
               meta={"mine": True, "paths": ["docs/adr/001-webhook-queue.md"]})
    no = {adr.id: DecisionVerdict(item_id=adr.id, is_decision=False, summary="nope")}
    d = find_decisions([adr], {ME}, ME, no)
    assert len(d) == 1 and d[0]["role"] == "led"
    weak = Item(id="gh:pr:o/r#10", source="github", type="pr", date="2026-09-04T00:00:00Z", title="Proposal: new cache", text="x", author=ME, meta={"mine": True})
    assert find_decisions([weak], {ME}, ME, {weak.id: DecisionVerdict(item_id=weak.id, is_decision=False, summary="")}) == []  # a weak candidate can be vetoed
    # three similar Slack threads with no ticket, PR or doc: no project
    import asyncio
    from eprofile import build
    from guards import budget_for
    th = [Item(id=f"slack:C1:{i}", source="slack", type="thread", date=f"2026-09-0{i}T00:00:00Z", title="#general thread", text="settlement batch timezone bug fix",
               author=ME, meta={"mine": True, "my_messages": 2, "channel": "general", "participants": [ME]}) for i in range(1, 4)]
    prof = asyncio.run(build(th, username=ME, names={ME}, llm=None, budget=budget_for("backfill"), framework="", role_and_level="", prior=None))
    assert prof["projects"] == [] and prof["project_of"] == {}


def test_slack_lists_private_channels_with_paging_and_names_the_cause_once():
    calls = []

    class Slack(Gateway):
        async def __call__(self, name, args):
            if name == "slack.list_channels":
                calls.append(args)
                if not args.get("cursor"):
                    return {"data": {"channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": "p2"}}}
                return {"data": {"channels": [{"id": "G9", "name": "uc10-test", "is_private": True, "is_member": True}], "response_metadata": {"next_cursor": ""}}}
            return await super().__call__(name, args)

    def run(chans, gw):
        src = GatewaySource(gw, ME, [], parse_channels(chans), False, ["PAY"], HAS, [], ["Parth Singh"])
        return asyncio.run(src.fetch(SINCE, None, ToolLog()))

    out = run("#uc10-test", Slack())
    assert [c["id"] for c in out["slack"]["channels"]] == ["C1", "G9"] and len(calls) == 2
    assert "private_channel" in calls[0]["types"] and "im" not in calls[0]["types"].split(",") and calls[0]["limit"] == 1000
    assert not [f for f in out["failed"] if f["source"].startswith("slack:#")]
    miss = run("#nope", Slack())
    errs = [f for f in miss["failed"] if f["source"] == "slack:#nope"]
    assert len(errs) == 1 and "not in the channel list" in errs[0]["error"] and "member" in errs[0]["error"]

    class NotMember(Gateway):
        async def __call__(self, name, args):
            if name == "slack.list_channels":
                return {"channels": [{"id": "G9", "name": "uc10-test", "is_private": True, "is_member": False}]}
            return await super().__call__(name, args)

    nm = run("uc10-test", NotMember())
    assert any("not a member" in f["error"] for f in nm["failed"])

