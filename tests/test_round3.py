"""Round-3 regression tests (LOCAL mocks only; schema-faithful for the GitHub listing tools; none of it is the official samples/UC-10 acceptance)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import agent
import ladder
from guards import Budget, BudgetExceeded, ToolLog
from ladder import (EvidenceMap, ExpectationPick, ExtractedExpectation, ExtractedFramework, Expectation, extract_with_model, map_evidence_with_model, statuses)
from models import Item
from sources import GatewaySource

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def item(n, title="Add retry to payouts", text="", type_="pr", **meta):
    return Item(id=f"i{n}", source="github", type=type_, date=f"2026-09-{1 + n % 28:02d}T10:00:00Z", title=title, text=text or f"body {n}", url=f"https://x/{n}", meta={"mine": True, **meta})


EXPS = [Expectation("E1", "current", "Delivers features independently"), Expectation("E2", "next", "Leads a cross-team technical decision"), Expectation("E3", "next", "Mentors other engineers")]


class Mapper:
    """A model that answers with whatever `rule(expectation, items)` returns, and records every request it was asked."""
    def __init__(self, rule, fail_on=()):
        self.rule, self.calls, self.fail_on = rule, [], set(fail_on)

    async def structured(self, schema, system, payload, budget=None):
        assert schema is EvidenceMap
        if budget is not None:
            budget.spend()
        self.calls.append(payload)
        if len(self.calls) in self.fail_on:
            raise RuntimeError("model unavailable")
        return EvidenceMap(picks=[ExpectationPick(expectation_id=e["expectation_id"], item_ids=self.rule(e, payload["items"])) for e in payload["expectations"]])


def mapping(llm, items, cache=None, exps=EXPS, budget=None):
    return asyncio.run(map_evidence_with_model(llm, budget or Budget(max_calls=150, max_seconds=600), exps, items, cache))


# ------------------------------------------------------------------------------------------- 1. evidence mapping by the model
def test_ids_are_validated_deduplicated_and_counted_by_code():
    items = [item(n) for n in range(6)]

    def rule(exp, shown):
        if exp["expectation_id"] == "E1":
            return ["i0", "i1", "i2", "i0", "i1", "FAKE-ID"]            # three real, two duplicates, one fabricated
        if exp["expectation_id"] == "E2":
            return ["i3", "i4", "not-in-this-request"]                    # two real, one fabricated
        return []
    r = mapping(Mapper(rule), items)
    rows = {g["id"]: g for g in statuses(EXPS, r.evidence, {i.id for i in items}, r.incomplete)}
    assert rows["E1"]["status"] == "backed" and rows["E1"]["count"] == 3 and len(set(rows["E1"]["evidence"])) == 3
    assert rows["E2"]["status"] == "thin" and rows["E2"]["count"] == 2
    assert rows["E3"]["status"] == "missing" and rows["E3"]["count"] == 0
    assert r.rejected == 2 and any("not real evidence ids" in x for x in r.reasons) and not r.incomplete


def test_a_fabricated_expectation_id_is_ignored():
    class L(Mapper):
        async def structured(self, schema, system, payload, budget=None):
            budget.spend()
            return EvidenceMap(picks=[ExpectationPick(expectation_id="E99", item_ids=["i0"]), ExpectationPick(expectation_id="E1", item_ids=["i0"])])
    r = mapping(L(lambda *a: []), [item(0)])
    assert set(r.evidence) == {"E1", "E2", "E3"} and r.evidence["E1"] == ["i0"] and r.rejected == 1


def test_an_id_from_another_request_does_not_count():
    items = [item(n) for n in range(ladder.MAP_BATCH + 5)]            # two requests
    first_ids = {}

    def rule(exp, shown):
        first_ids.setdefault(len(first_ids), shown[0]["item_id"])
        return ["i0"] if exp["expectation_id"] == "E1" else []        # i0 exists, but only one of the requests showed it
    r = mapping(Mapper(rule), items)
    assert r.evidence["E1"] == ["i0"] and r.rejected == 1


def test_the_model_sees_real_evidence_text_and_the_choice_is_not_keyword_matching():
    misleading = item(0, title="Delivers features independently", text="renamed a variable")                 # shares the expectation's words, shows nothing
    real = item(1, title="PAY-9 retry storm", text="I ran the incident call, wrote the fix and shipped it end to end without help")  # shares none of its words
    llm = Mapper(lambda e, shown: ["i1"] if e["expectation_id"] == "E1" else [])
    r = mapping(llm, [misleading, real])
    sent = {i["item_id"]: i for i in llm.calls[0]["items"]}
    assert "I ran the incident call" in sent["i1"]["text"] and sent["i1"]["title"] == "PAY-9 retry storm"
    assert r.evidence["E1"] == ["i1"]                                                                         # chosen by the model on its text, not by shared words


def test_only_the_users_countable_work_is_sent():
    items = [item(0), item(1, reverted=True), item(2, is_revert=True), item(3, generated_only=True), item(4, mine=False, type_="ticket"), item(5, title="Initial commit", type_="commit"),
             item(6, mine=False, type_="review")]
    items[4].meta["mine"] = False
    llm = Mapper(lambda e, shown: [])
    mapping(llm, items)
    assert {i["item_id"] for i in llm.calls[0]["items"]} == {"i0", "i6"}   # a review of someone else's PR is the user's own evidence; reverts, lockfile PRs, trivial commits and others' items are not


# ----- failure is "unavailable", never "missing"
def test_no_model_means_unavailable_not_missing():
    items = [item(n) for n in range(4)]
    r = mapping(None, items)
    rows = statuses(EXPS, r.evidence, {i.id for i in items}, r.incomplete)
    assert r.incomplete and {g["status"] for g in rows} == {"unavailable"} and "no model" in " ".join(r.reasons)


def test_model_error_and_budget_exhaustion_are_unavailable_with_a_reason():
    items = [item(n) for n in range(4)]
    bad = mapping(Mapper(lambda *a: [], fail_on={1}), items)
    assert bad.incomplete and "failed" in " ".join(bad.reasons) and {g["status"] for g in statuses(EXPS, bad.evidence, {i.id for i in items}, True)} == {"unavailable"}
    out = mapping(Mapper(lambda *a: ["i0"]), items, budget=Budget(max_calls=0, max_seconds=100))
    assert out.incomplete and out.new_calls == 0 and "no model" not in " ".join(out.reasons)
    assert {g["status"] for g in statuses(EXPS, out.evidence, {i.id for i in items}, True)} == {"unavailable"}


def test_partial_mapping_keeps_proven_backed_and_marks_the_rest_unavailable():
    items = [item(n) for n in range(ladder.MAP_BATCH * 2)]
    llm = Mapper(lambda e, shown: [i["item_id"] for i in shown][:3] if e["expectation_id"] == "E1" else [], fail_on={2})   # the second request fails
    r = mapping(llm, items)
    rows = {g["id"]: g["status"] for g in statuses(EXPS, r.evidence, {i.id for i in items}, r.incomplete)}
    assert r.incomplete and rows == {"E1": "backed", "E2": "unavailable", "E3": "unavailable"}


# ----- budget
def test_mapping_stays_inside_the_runs_model_budget():
    items = [item(n) for n in range(ladder.MAP_BATCH * 5)]
    b = Budget(max_calls=7, max_seconds=100)               # 7 - reserve 4 = 3 requests may be spent on mapping
    llm = Mapper(lambda *a: [])
    r = mapping(llm, items, budget=b)
    assert len(llm.calls) == 3 and b.calls == 3 <= b.max_calls and r.incomplete and "mapped on the next run" in " ".join(r.reasons)


# ----- cache and invalidation
def test_cache_is_reused_and_invalidated_by_framework_or_evidence_changes():
    items = [item(n) for n in range(5)]
    rule = lambda e, shown: [i["item_id"] for i in shown][:3] if e["expectation_id"] == "E1" else []
    first_llm = Mapper(rule)
    first = mapping(first_llm, items)
    assert len(first_llm.calls) == 1
    again = Mapper(rule)
    same = mapping(again, items, cache=first.cache)
    assert again.calls == [] and same.reused == 5 and same.evidence == first.evidence and not same.incomplete          # nothing changed: no model call
    changed = [*items[:4], item(4, text="rewritten body that now shows something else")]
    l2 = Mapper(rule)
    mapping(l2, changed, cache=first.cache)
    assert [i["item_id"] for i in l2.calls[0]["items"]] == ["i4"]                                                    # only the changed item goes back
    grown = [*items, item(5)]
    l3 = Mapper(rule)
    mapping(l3, grown, cache=first.cache)
    assert [i["item_id"] for i in l3.calls[0]["items"]] == ["i5"]                                                    # only the new item
    shrunk = mapping(Mapper(rule), items[:3], cache=first.cache)
    assert all(i in {"i0", "i1", "i2"} for ids in shrunk.evidence.values() for i in ids)                              # an item that is gone no longer counts
    l4 = Mapper(rule)
    mapping(l4, items, cache=first.cache, exps=[*EXPS[:2], Expectation("E3", "next", "Mentors other engineers through pairing")])
    assert len(l4.calls[0]["items"]) == 5                                                                            # the framework changed: everything is asked again


# ------------------------------------------------------------------------------------------- 2. extraction grounded by a quote
FRAMEWORK = ("At Software Engineer II you are expected to ship features without supervision and to give careful code review. "
             "To reach Senior Software Engineer you should lead a cross-team technical decision and coach other engineers.")


class Extractor:
    def __init__(self, lines, fail=False):
        self.lines, self.fail, self.n = lines, fail, 0

    async def structured(self, schema, system, payload, budget=None):
        assert schema is ExtractedFramework
        budget.spend()
        self.n += 1
        if self.fail:
            raise RuntimeError("model unavailable")
        return ExtractedFramework(expectations=[ExtractedExpectation(text=t, level=lv, source_quote=q) for t, lv, q in self.lines])


def extract(llm, text=FRAMEWORK, budget=None):
    return asyncio.run(extract_with_model(llm, budget or Budget(max_calls=10, max_seconds=100), text, "Software Engineer II"))


def test_supported_paraphrases_are_accepted_with_their_quote_and_level():
    exps, why, rejected = extract(Extractor([
        ("Delivers work independently", "current", "ship features without supervision"),                     # paraphrase of the quoted passage
        ("Reviews code carefully", "current", "give careful   CODE review"),                                  # whitespace and case differences in the quote are ignored
        ("Leads cross-team technical decisions", "next", "lead a cross-team technical decision"),
        ("Coaches engineers", "next", "coach other engineers")]))
    assert [(e.level, e.text) for e in exps] == [("current", "Delivers work independently"), ("current", "Reviews code carefully"),
                                                 ("next", "Leads cross-team technical decisions"), ("next", "Coaches engineers")]
    assert exps[0].quote == "ship features without supervision" and rejected == 0 and why == ""


def test_invented_expectations_are_rejected_even_when_their_words_overlap_the_framework():
    exps, why, rejected = extract(Extractor([
        ("Delivers work independently", "current", "ship features without supervision"),
        ("Leads technical decisions across the company and the board", "next", "lead technical decisions across the company"),   # words overlap, no such passage
        ("Runs the quarterly offsite", "next", "organise the quarterly offsite"),
        ("Mentors", "next", "coach"),                                                                           # a single word is not a passage
        ("Anything", "next", "")]))
    assert [e.text for e in exps] == ["Delivers work independently"] and rejected == 4


def test_no_grounded_expectation_is_an_explicit_failure_and_a_model_error_is_reported():
    exps, why, rejected = extract(Extractor([("Invented", "next", "this sentence is not in the framework")]))
    assert exps == [] and "no expectation whose source quote appears" in why and rejected == 1
    exps, why, _ = extract(Extractor([], fail=True))
    assert exps == [] and "model call failed" in why
    exps, why, _ = extract(Extractor([]), budget=Budget(max_calls=0, max_seconds=100))
    assert exps == [] and "limit" in why


def test_long_frameworks_are_read_in_parts_and_a_quote_from_any_part_is_valid():
    long = ("Intro text. " * 1200) + " To reach Senior you should lead a cross-team technical decision. " + ("More text. " * 2000)
    assert len(long) > ladder.WINDOW * 2
    llm = Extractor([("Leads cross-team decisions", "next", "lead a cross-team technical decision")])
    exps, why, _ = extract(llm, long)
    assert len(exps) == 1 and llm.n >= 2                       # every part was read; the quote is checked against the whole framework
    too_long = "x " * (ladder.WINDOW * (ladder.MAX_WINDOWS + 1) // 2)
    _, why, _ = extract(Extractor([("A b", "next", "x x")]), too_long)
    assert "were not read" in why


# ------------------------------------------------------------------------------------------- 3. GitHub owner listing (schema-faithful mocks)
class GitHubListing:
    """Mirrors the two tool schemas: org tool needs `org`; user tool needs `username`; both take integer `page` and `per_page` (max 100).
    Without per_page the org tool gives 1 row and the user tool 30 (their real defaults), so a missing per_page shows up as truncation."""
    def __init__(self, org_pages=None, user_pages=None, org_error=None, org_payload=None, repeat=False):
        self.org_pages, self.user_pages, self.org_error, self.org_payload, self.repeat = org_pages or {}, user_pages or {}, org_error, org_payload, repeat
        self.calls = []

    async def __call__(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "github.search_issues":
            return {"items": []}
        if name not in ("github.list_organization_repositories", "github.list_repositories_for_a_user"):
            return []
        org = name.endswith("organization_repositories")
        required, other = ("org", "username") if org else ("username", "org")
        assert required in args and other not in args, f"{name} needs {required!r}, got {sorted(args)}"
        assert set(args) <= {required, "page", "per_page"}, sorted(args)
        assert isinstance(args.get("page", 1), int) and isinstance(args.get("per_page", 1), int) and args.get("per_page", 1) <= 100
        if org and self.org_error:
            raise RuntimeError(self.org_error)
        if org and self.org_payload:
            return self.org_payload
        page, per = args.get("page", 1), args.get("per_page", 1 if org else 30)
        pages = self.org_pages if org else self.user_pages
        if self.repeat:
            return {"repositories": [{"full_name": f"{args[required]}/same-{i}"} for i in range(per)]}
        want = pages.get(page, 0)
        return {"repositories": [{"full_name": f"{args[required]}/r{page}-{i}"} for i in range(min(want, per))]}


def fetch_with(call, repos, tools=("github.list_organization_repositories", "github.list_repositories_for_a_user")):
    src = GatewaySource(call, "u", repos, [], False, [], {"github.search_issues", "github.list_commits", *tools})
    return src, asyncio.run(src.fetch("2026-04-01T00:00:00Z", None, ToolLog()))


def test_org_listing_sets_per_page_reads_to_the_empty_page_and_claims_no_private_completeness():
    call = GitHubListing()
    _, out = fetch_with(call, ['acme'])
    assert out['blocked'] and 'disabled' in str(out['failed'])
    assert not call.calls


def test_only_a_confirmed_not_an_org_answer_falls_back_to_the_user_listing_and_discloses_public_only():
    call = GitHubListing()
    _, out = fetch_with(call, ['acme'])
    assert out['blocked'] and 'disabled' in str(out['failed'])
    assert not call.calls


@pytest.mark.parametrize("err", ["403 Forbidden: Resource not accessible by integration", "401 Unauthorized", "Bad credentials", "429 rate limit exceeded", "ConnectionError: timed out",
                                 "503 Service Unavailable", "SAML enforcement: token not authorized", "missing scope read:org"])
def test_auth_permission_network_errors_never_fall_back_to_a_user_listing(err):
    call = GitHubListing()
    _, out = fetch_with(call, ['acme'])
    assert out['blocked'] and 'disabled' in str(out['failed'])
    assert not call.calls


def test_an_org_with_no_visible_repositories_is_reported_not_treated_as_a_user():
    call = GitHubListing()
    _, out = fetch_with(call, ['acme'])
    assert out['blocked'] and 'disabled' in str(out['failed'])
    assert not call.calls


def test_a_repeated_nonempty_page_blocks_the_run_as_incomplete():
    call = GitHubListing()
    _, out = fetch_with(call, ['acme'])
    assert out['blocked'] and 'disabled' in str(out['failed'])
    assert not call.calls


def test_a_listing_that_never_ends_is_incomplete(monkeypatch):
    call = GitHubListing()
    _, out = fetch_with(call, ['acme'])
    assert out['blocked'] and 'disabled' in str(out['failed'])
    assert not call.calls


def test_explicit_repos_and_mixed_entries_are_read_without_any_listing():
    call = GitHubListing()
    src, out = fetch_with(call, ["acme/api", "ACME/API", "acme/web"])
    assert src.repos == ["acme/api", "acme/web"] and not any(n in ("github.list_organization_repositories", "github.list_repositories_for_a_user") for n, _ in call.calls)


def test_a_missing_tool_is_reported_not_guessed():
    call = GitHubListing()
    _, out = fetch_with(call, ['acme'])
    assert out['blocked'] and 'disabled' in str(out['failed'])
    assert not call.calls


# ------------------------------------------------------------------------------------------- 4. the requested period is never silently shortened
class Src:
    def __init__(self):
        self.since = []

    async def fetch(self, since, until, log):
        self.since.append(since)
        return {"github": {"prs": [], "reviews": {}, "commits": []}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": [], "failed": [], "capped": []}


def run_months(months, now=NOW):
    src = Src()
    deps = agent.Deps(lambda cfg: src, llm=None, now=lambda: now)
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {"github_username": "u", "repos_or_org": "o/r", "role_and_level": "SE", "backfill_months": months}, "memory": {}}))
    return out, src


@pytest.mark.parametrize("months", [6, 120, 1200, 12000, 13200])
def test_every_representable_month_count_reads_exactly_that_period(months):
    out, src = run_months(months)
    from inputs import backfill_since
    assert src.since == [backfill_since(NOW, months)]
    assert "1970" not in str(out["result"]["notes"]) and out["result"]["status"] == "completed"


def test_years_before_1000_are_zero_padded_so_the_date_stays_parseable():
    out, src = run_months(13200)                     # 1084 years back: year 942
    assert src.since[0].startswith("0926-") and datetime.fromisoformat(src.since[0].replace("Z", "+00:00")).year == 926


def test_the_largest_representable_request_works_and_one_more_is_refused_before_any_call():
    most = (NOW.year - 1) * 12 + NOW.month - 1
    out, src = run_months(most)
    assert src.since and src.since[0].startswith("0001-")
    for months in (most + 1, 10 ** 9, 10 ** 15):
        srcs = Src()
        deps = agent.Deps(lambda cfg: srcs, llm=None, now=lambda: NOW)
        with pytest.raises(ValueError, match="earliest date this agent can compute") as e:
            asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {"github_username": "u", "repos_or_org": "o/r", "role_and_level": "SE", "backfill_months": months}, "memory": {}}))
        assert srcs.since == [] and str(most) in str(e.value)
