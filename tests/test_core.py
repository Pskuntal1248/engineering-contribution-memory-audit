import fixtures
import pytest
from collections import Counter

import linking
import metrics
from guards import ToolLog, Statement, cap_role, keep_cited, drop_colleague_assessments, scrub, internal_terms, flag_confidential, confidential_ids, FLAG, predicts_promotion
from ladder import parse_framework, statuses
from pipeline import Config, ingest
from weekly import new_items, id_key
import ledger


@pytest.fixture(scope="module")
def data():
    return fixtures.build()


def cfg(**kw):
    base = dict(username=fixtures.USER, repos=["acme/payments-api", "acme/web"], jira_site=fixtures.SITE, jira_projects=["PAY", "WEB"],
                slack_channels=["payments-eng", "incidents", "web-platform", "random", "design", "oncall", "standup", "releases", "help-eng", "data", "security", "ops"],
                slack_user_ids={fixtures.ME}, name_hints=["Meera Joshi", fixtures.USER])
    base.update(kw)
    return Config(**base)


@pytest.fixture(scope="module")
def ing(data):
    log = ToolLog()
    res = ingest(data, cfg(), log)
    return res, log


def test_bots_and_noise_removed(ing):
    res, _ = ing
    assert not any("[bot]" in i.author for i in res.items)
    assert all(i.meta.get("mine") or i.type == "review" for i in res.items if i.type != "thread" and i.type != "doc")


def test_reverted_prs_not_counted_as_shipped(data, ing):
    res, _ = ing
    by = {i.id: i for i in res.items}
    exp = data["expected"]["reverted_prs"]
    assert len(exp) == 6
    for rid in exp:
        assert by[rid].meta.get("reverted") and not metrics.shipped(by[rid])
    stats = metrics.pr_stats(res.items)
    assert stats["prs_reverted"] == 6
    # revert PRs themselves are not shipped either
    assert all(not metrics.shipped(i) for i in res.items if i.type == "pr" and i.meta.get("is_revert"))


def test_lockfile_pr_not_top_by_size(data, ing):
    res, _ = ing
    lock = {i.id: i for i in res.items}[data["expected"]["lockfile_pr"]]
    assert lock.meta["size"]["excluded"] >= 3900 and lock.meta["size"]["lines"] < 20
    top = metrics.top_contributions(res.items, 10)
    assert lock.id not in [t.id for t in top]


def test_missing_tests_improvement_area(data, ing):
    res, _ = ing
    areas = metrics.improvement_areas(res.items)
    tests = [a for a in areas if a["theme"] == "tests"]
    assert tests and tests[0]["count"] == 11 and len(tests[0]["item_ids"]) >= 3
    assert set(tests[0]["prs"]) == set(data["expected"]["missing_tests_prs"])


def test_no_dm_reads_when_excluded(ing):
    res, log = ing
    assert log.dm_reads() == []
    assert not any(i.meta.get("is_dm") for i in res.items)
    assert res.dm_dropped == 0  # never fetched in the first place


def test_dm_included_only_when_opted_in(data):
    log = ToolLog()
    res = ingest(data, cfg(slack_channels=["payments-eng", "dm-arjun"], include_dms=True), log)
    assert any(i.meta.get("is_dm") for i in res.items)
    assert log.dm_reads()


def test_projects_grouped_80_percent(data, ing):
    res, _ = ing
    clusters = linking.link([i for i in res.items], {})
    where = {iid: pid for pid, c in clusters.items() for iid in c["item_ids"]}
    for p, spec in data["expected"]["projects"].items():
        ids = [i for i in spec["items"] if i in where]
        ctr = Counter(where[i] for i in ids)
        best, n = ctr.most_common(1)[0]
        frac = n / len(spec["items"])
        print(p, spec["name"], round(frac, 2))
        assert frac >= 0.8, (p, frac)


def test_invisible_work_threads_present_and_unlinked(data, ing):
    res, _ = ing
    by = {i.id: i for i in res.items}
    for sid in data["expected"]["invisible"]:
        assert sid in by and not by[sid].keys


def test_week2_adds_only_new_items(data):
    full = ingest(data, cfg())
    start = data["expected"]["week2_start"]
    seen = {id_key(i.id) for i in full.items if i.date < start}
    fresh, skipped = new_items(full.items, seen)
    new_ids = {i.id for i in fresh}
    exp = set(data["expected"]["week2_ids"])
    assert exp <= new_ids
    assert len(fresh) == len(exp) == 14, (len(fresh), sorted(new_ids - exp)[:5])
    # running the same week again adds nothing
    again, _ = new_items(full.items, seen | {id_key(i) for i in new_ids})
    assert again == []


def test_confidential_doc_flagged(data, ing):
    res, _ = ing
    conf = confidential_ids(res.items)
    assert data["expected"]["confidential_doc"] in conf
    st = flag_confidential(Statement("Priced the renewal", [data["expected"]["confidential_doc"]]), conf)
    assert st.text.startswith(FLAG)


def test_ladder_statuses(data, ing):
    res, _ = ing
    exps = parse_framework(data["expected"]["ladder_text"], "Software Engineer II", "SE III")
    nxt = [e for e in exps if e.level == "next"]
    assert len(nxt) == 8
    known = {i.id for i in res.items}
    pool = [i.id for i in res.items if i.type == "pr"]
    ev = {nxt[0].id: pool[:5], nxt[1].id: pool[5:6], nxt[2].id: ["not-a-real-id", pool[0], pool[0]]}  # backed, thin, one real item + a fabricated id + a duplicate
    rows = {r["expectation"]: r for r in statuses(nxt, ev, known)}
    assert rows[nxt[0].text]["status"] == "backed" and rows[nxt[1].text]["status"] == "thin" and rows[nxt[2].text] ["status"] == "thin" and rows[nxt[2].text]["count"] == 1
    assert rows[nxt[3].text]["status"] == "missing"                       # nothing was mapped to it
    assert {r["status"] for r in statuses(nxt, {}, known, incomplete=True)} == {"unavailable"}   # an incomplete mapping is never "missing"
    assert statuses(nxt[:1], {nxt[0].id: pool[:3]}, known, incomplete=True)[0]["status"] == "backed"  # three real items stay backed


def test_every_statement_needs_known_evidence():
    out, dropped = keep_cited([Statement("a", ["x"]), Statement("b", ["nope"]), Statement("c", [])], {"x"})
    assert [s.text for s in out] == ["a"] and dropped == 2


def test_roles_never_inflated(data, ing):
    res, _ = ing
    revs = [i for i in res.items if i.type == "review" and i.meta.get("mine")][:3]
    assert cap_role("led", revs, fixtures.USER) == "reviewed"
    prs = [i for i in res.items if i.type == "pr" and i.meta.get("mine")][:2]
    assert cap_role("led", prs, fixtures.USER) in {"led", "built", "contributed"}
    assert cap_role("led", [], fixtures.USER) in {"supported", "reviewed"}


def test_no_colleague_assessment():
    keep, dropped = drop_colleague_assessments([Statement("Arjun is slow at reviews", ["a"]), Statement("Paired with Arjun on the fix", ["a"])], ["Arjun Rao"])
    assert [s.text for s in keep] == ["Paired with Arjun on the fix"] and dropped == 1


def test_no_promotion_prediction():
    assert predicts_promotion("You will likely be promoted this cycle")
    assert not predicts_promotion("No evidence yet of leading a cross-team decision")


def test_interview_stories_scrubbed(data, ing):
    res, _ = ing
    terms = internal_terms(res.items, data["expected"]["customers"], "acme", fixtures.SITE)
    out = scrub("Fixed payouts for Globex Foods in acme/payments-api saving $40,000 and 37 to 0 duplicates", terms)
    assert "Globex" not in out and "payments-api" not in out and "40,000" not in out and "acme" not in out.lower()


def test_ledger_columns_first_and_csv(ing):
    res, _ = ing
    rows, trunc = ledger.rows(res.items, {})
    assert ledger.COLUMNS[:4] == ["source", "date", "project", "type"] and not trunc
    csv_text = ledger.to_csv(rows)
    assert csv_text.splitlines()[0].startswith("source,date,project,type")
