"""Regression tests for the 0.1.4 accuracy fixes:
1. the user must never appear as their own collaborator, even when only their GitHub username
   is known (display name learned from provable-self Slack evidence);
2. Problems solved reads the PR/ticket BODY for problem evidence, not only the title;
3. the form: optional knobs grouped under "Advanced:", the report selector near the top with a
   label that says what updates the profile vs what only reads it, the weekly run stated as
   Friday 17:00 IST, and the timezone input labeled date-ranges-only.
"""
import asyncio

import yaml

from eprofile import build
from guards import budget_for, self_aliases
from models import Item
from outputs import corpus_for

ME = "Pskuntal1248"
R = "Pskuntal1248/uc10-synthetic-test-data"


def pr(n, key, title, body="", co=()):
    return Item(id=f"gh:pr:{R}#{n}", source="github", type="pr", date="2026-10-10T08:00:00Z", title=f"{key} {title}",
                url=f"https://github.com/{R}/pull/{n}", text=body, author=ME, co_authors=list(co), keys=[key],
                meta={"mine": True, "repo": R, "number": n, "merged_flag": True, "state": "closed"})


def ticket(key, title, ts="2026-10-10T07:00:00Z"):
    return Item(id=f"jira:{key}", source="jira", type="ticket", date=ts, title=f"{key} {title}",
                url=f"https://x.atlassian.net/browse/{key}", text=title, author="Parth Singh", keys=[key],
                meta={"mine": True, "status": "Done"})


def my_thread(text, keys=()):
    return Item(id="slack:G1:179001.000100", source="slack", type="thread", date="2026-10-10T09:00:00Z", title="#uc10-test thread",
                url="https://s/p1", text=f"Parth Singh: {text}", author="Parth Singh", keys=list(keys),
                meta={"channel": "uc10-test", "channel_id": "G1", "messages": 2, "my_messages": 1, "participants": ["Parth Singh"],
                      "mine": True, "root_is_mine": True, "my_names": ["Parth Singh"]})


def profile(items, names):
    return asyncio.run(build(items, username=ME, names=names, llm=None, budget=budget_for("backfill"), framework="",
                             role_and_level="Software Engineer", prior=None))


# --- 1. self-collaborator exclusion

def test_normalize_thread_records_the_users_own_display_names():
    import normalize as N
    msgs = [{"ts": "1791641890.36", "user": "U1", "user_name": "Parth Singh", "text": "I fixed the window builder"},
            {"ts": "1791641900.36", "user": "U2", "user_name": "Sam Lee", "text": "thanks!"}]
    it = N.normalize_thread("uc10-test", "G1", msgs, {"U1"}, "https://x.slack.com")
    assert it.meta["my_names"] == ["Parth Singh"] and it.meta["root_is_mine"] is True


def test_self_aliases_only_from_provable_self_evidence():
    mine = my_thread("hello")
    other = Item(id="slack:G1:2", source="slack", type="thread", date="2026-10-10T09:00:00Z", title="t", url="",
                 text="x", author="Sam Lee", meta={"mine": True, "my_messages": 0, "participants": ["Sam Lee", "Parth Singh"]})
    got = self_aliases([mine, other], ME, set())
    assert "Parth Singh" in got and "Sam Lee" not in got and ME in got


def test_user_is_never_their_own_collaborator_even_without_display_name_input():
    items = [ticket("PAY-1", "Add idempotency key to payouts"), pr(1, "PAY-1", "Add idempotency key to payouts", co=["Sam Lee"]),
             my_thread("PAY-1 idempotency keys are live in staging", keys=("PAY-1",))]
    prof = profile(items, {ME})  # display name NOT given: the alias is learned from the Slack evidence
    cos = sorted({c for p in prof["projects"] for c in p["co_contributors"]})
    assert "Parth Singh" not in cos, cos
    assert "Sam Lee" in cos, cos  # a real collaborator is never eaten by the exclusion


# --- 2. Problems solved reads bodies, and only problem-evidence items

def page(items, names={ME, "Parth Singh"}):
    prof = profile(items, names)
    corpus, _ = corpus_for("backfill", prof, items, title="t", terms=set(), keep_internal=False, colleagues=[], names=names,
                           date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set())
    return {s["heading"]: s for s in corpus["sections"]}


def test_pr_body_problem_is_listed_with_its_source_sentence():
    items = [ticket("PAY-3", "Add payout reconciliation job"),
             pr(4, "PAY-3", "Add payout reconciliation job", body="Settlement totals did not match the bank file. Adds a nightly reconciliation job.")]
    solved = page(items)["Problems solved"]["body"]
    assert "Source describes: Settlement totals did not match the bank file." in solved


def test_feature_pr_without_problem_evidence_is_not_called_a_fix():
    items = [ticket("SRCH-1", "Add query parser"), pr(3, "SRCH-1", "Add query parser", body="Adds quoted phrases and exclusions to product search.")]
    solved = page(items)["Problems solved"]["body"]
    assert "query parser" not in solved.lower()


def test_title_fix_still_listed():
    items = [ticket("PAY-9", "Fix payout duplicate creation"), pr(9, "PAY-9", "Fix payout duplicate creation")]
    solved = page(items)["Problems solved"]["body"]
    assert "PAY-9" in solved


# --- 3. the form

M = yaml.safe_load(open("manifest.yaml"))
BY = {i["name"]: i for i in M["inputs"]}


def test_description_states_friday_17_ist_within_500_chars():
    assert len(M["description"]) <= 500
    assert "Friday 17:00 IST" in M["description"]


def test_report_selector_is_before_the_advanced_group_and_explains_update_vs_read_only():
    names = [i["name"] for i in M["inputs"]]
    first_advanced = next(k for k, n in enumerate(names) if BY[n]["label"].startswith("Advanced:"))
    assert names.index("request") < first_advanced
    label = BY["request"]["label"]
    assert "the scheduled run uses this" not in label
    assert "weekly_update updates the saved profile" in label and "only read the saved profile" in label
    assert BY["request"]["default"] == "weekly_update"  # saved-input/default semantics unchanged


def test_advanced_group_keeps_the_framework_team_and_redaction_options_optional():
    advanced = {n for n, i in BY.items() if i["label"].startswith("Advanced:")}
    assert {"review_framework", "team_changes", "redact_terms", "keep_internal_details"} <= advanced
    for n in ("review_framework", "team_changes", "redact_terms", "keep_internal_details"):
        assert BY[n]["required"] is False, n
    assert BY["keep_internal_details"]["default"] == "No"
    for n in ("repos_or_org", "jira_projects", "slack_channels", "role_and_level", "request", "github_username"):
        assert not BY[n]["label"].startswith("Advanced:"), n  # the core group stays unprefixed


def test_timezone_is_kept_and_labeled_date_ranges_only():
    tz = BY["timezone"]
    assert tz["label"] == "Advanced: Your timezone, used for date ranges only; schedules run in IST"
    assert tz["default"] == "Asia/Kolkata"


# --- 4. problem LANGUAGE without resolution is not claimed as solved; excluded items stay out

def solved_body(items, names={ME, "Parth Singh"}):
    return page(items, names)["Problems solved"]["body"]


def test_unresolved_problem_language_is_not_presented_as_solved():
    bodies = [
        "Adds retry metrics. Known bug remains unresolved in the retry path.",
        "Adds a backoff experiment. Failed experiment, not fixed.",
        "Adds payout ids. Future work: fix duplicate payouts.",
    ]
    for k, body in enumerate(bodies):
        items = [ticket(f"PAY-1{k}", f"Add probe {k}"), pr(10 + k, f"PAY-1{k}", f"Add probe {k}", body=body)]
        assert "probe" not in solved_body(items).lower(), body


def test_reverted_and_lockfile_only_prs_are_not_problems_solved():
    reverted = pr(5, "SRCH-2", "Add result ranking", body="Ranking failed for short queries.")
    reverted.meta["reverted"] = True
    lockfile = pr(8, "CHORE-1", "Update dependencies (lockfile only)", body="Fix lockfile mismatch with upstream.")
    lockfile.meta["generated_only"] = True
    items = [ticket("SRCH-2", "Add result ranking"), reverted, ticket("CHORE-1", "Update dependencies"), lockfile]
    body = solved_body(items)
    assert "ranking" not in body.lower() and "lockfile" not in body.lower()


# --- 5. old saved items (pre-fix threads carry no my_names)

def test_old_rooted_thread_still_teaches_the_alias_and_saved_profile_self_attribution_ends():
    old_thread = Item(id="slack:G1:9", source="slack", type="thread", date="2026-10-10T09:00:00Z", title="#uc10-test thread",
                      url="https://s/p9", text="Parth Singh: PAY-1 idempotency keys are live in staging", author="Parth Singh",
                      keys=["PAY-1"], meta={"channel": "uc10-test", "channel_id": "G1", "messages": 2, "my_messages": 1,
                                            "participants": ["Parth Singh"], "mine": True, "root_is_mine": True})  # no my_names: saved before the fix
    items = [ticket("PAY-1", "Add idempotency key to payouts"), pr(1, "PAY-1", "Add idempotency key to payouts", co=["Sam Lee"]), old_thread]
    sects = page(items, {ME})
    projects = sects["Projects"]["body"]
    assert "with Parth Singh" not in projects
    assert "with Sam Lee" in projects
