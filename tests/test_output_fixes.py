"""Fixes from the user's real agents-testing profile (GitHub only). Fixtures are the user's own commit titles and messages; everything here is local, pure logic."""
import sys
sys.path.insert(0, "src")
from analysis import commit_fix, map_skills
from models import Item
from normalize import normalize_commit, split_co_authors
from outputs import corpus_for, _project_facts
from report_notes import split_notes
from test_real_output import ME, NAMES, R, profile

E742425 = ("Stop double-counting rebuildAndRedeploy's internal restart\n\nrebuildAndRedeploy() called the public restartApplication() tool\n"
           "internally to load newly compiled code, and that method wrote its own\ntranscript entry. Since turnsUsed is read directly from the transcript, a rebuild+restart -- one decision by\n"
           "the agent -- was billed as two turns. The internal call is now exempt, so one rebuild costs one turn.")
B6BC12F = ("Fix agent resolution pipeline and evaluator scoring\n\n- Pass in-container health URL (localhost:8080) to the agent instead of\n  the host-mapped port, which was unreachable from inside the container\n"
           "  and caused curl exit=7 on every health check.\n- Wait for the app to become healthy after restartApplication() so the\n  agent doesn't check health mid-startup and give up.\n\n"
           "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>")


def citem(sha, msg, files=None):
    raw = {"sha": sha, "html_url": f"https://github.com/{R}/commit/{sha}", "commit": {"message": msg, "author": {"name": "Parth", "date": "2026-08-18T10:00:00Z"}},
           "author": {"login": ME}, "files": files}
    x = normalize_commit(raw, R, ME, [])
    assert isinstance(x, Item)
    return x


def test_zero_failed_embeddings_note_is_info_and_a_real_failure_is_not():
    info, _, fail = split_notes(["topic grouping used embeddings: all 132 items in 3 request(s), 0 failed, each charged to the run's model budget, plus keywords"])
    assert info and not fail
    info, _, fail = split_notes(["topic grouping used embeddings: all 132 items in 3 request(s), 2 failed, each charged"])
    assert fail and not info


def test_one_not_covered_line_per_source():
    info, _, _ = split_notes(["Not covered: Slack (no channels were listed)", "Not covered: Slack (no channels given)", "Not covered: Jira (no project keys given)"])
    assert [n for n in info if "Slack" in n] == ["Not covered: Slack (no channels were listed)"] and len(info) == 2


def test_skills_have_no_false_positives_from_the_users_commits():
    def skills(title, paths=None):
        return {k["skill"] for k in map_skills([Item(id=title, source="github", type="commit", date="2026-08-01", title=title, text=title, meta={"mine": True, "paths": paths or []})])}
    assert "technical design" not in skills("Keep session design docs local-only via gitignore", [".gitignore"])
    assert "technical design" not in skills("Update .gitignore to allow tracking of design document")
    assert "technical design" not in skills("Fix decision tree rendering")
    assert "accessibility" not in skills("feat: integrate Tailwind CSS and refactor styles")
    assert "frontend" in skills("feat: integrate Tailwind CSS and refactor styles")
    assert "technical design" in skills("Add ADR 0004: choose Kafka over SQS", ["docs/adr/0004-kafka.md"])  # one real artifact is enough
    assert "technical design" not in skills("Add ADR 0004: choose Kafka over SQS", ["src/main.py"])  # the files say it is not a design doc
    assert "accessibility" in skills("Add aria-label and keyboard navigation to the run table")


def test_ai_co_author_is_credited_as_ai_and_kept_apart_from_people():
    people, ai = split_co_authors("x\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>\nCo-authored-by: Asha Rao <asha@x.com>")
    assert people == ["Asha Rao"] and ai == ["Claude Opus 4.8"]
    c = citem("b6bc12fee6801d516b31cc6ae109bf7cc65f77d4", B6BC12F)
    assert c.co_authors == [] and c.meta["ai_assist"] == ["Claude Opus 4.8"]
    facts = _project_facts({"role": "built", "co_contributors": [], "ai_assist": ["Claude Opus 4.8"], "goal": "", "teams": []})
    assert facts[0] == "Role: built" and any(f.startswith("AI assistant credited") and "not a human collaborator" in f for f in facts)


def test_problems_solved_uses_commit_bodies_but_not_titles_or_typos():
    assert commit_fix(*(lambda m: (m.split("\n")[0], m))(E742425))
    assert commit_fix(*(lambda m: (m.split("\n")[0], m))(B6BC12F))
    assert commit_fix("Fix typo in readme", "Fix typo in readme\n\nFixed a typo that failed to read well in the quickstart and the setup guide section.") is None
    assert commit_fix("Fix login bug", "Fix login bug") is None  # a title alone is not evidence
    assert commit_fix("chore: update dependencies and enhance styling", "chore: update dependencies and enhance styling\n\n- Added new dependencies: radix-ui and playwright.\n- Refined CSS styles for a cleaner look.") is None
    items = [citem("e742425aaaa", E742425), citem("02baba5bbbb", "chore: update dependencies and enhance styling\n\n- Added new dependencies: radix-ui.\n- Refined CSS styles for a cleaner look and updated palette.")]
    corpus, _ = corpus_for("backfill", profile(items), items, title="t", terms=set(), keep_internal=False, colleagues=[], names=NAMES,
                           date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set(), coverage_status={"github": "read", "jira": "not requested", "slack": "not requested", "docs": "not requested"})
    sec = next(s for s in corpus["sections"] if s["heading"] == "Problems solved")
    text = str(sec)
    assert "Stop double-counting" in text and "chore: update" not in text
    inv = next(s for s in corpus["sections"] if s["heading"] == "Invisible work")
    assert "Slack (not requested)" in str(inv) and "None found:" not in str(inv)


def test_ledger_footer_says_pr_commits_are_not_separate_rows():
    items = [citem("e742425aaaa", E742425)]
    corpus, _ = corpus_for("backfill", profile(items), items, title="t", terms=set(), keep_internal=False, colleagues=[], names=NAMES,
                           date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set())
    led = next(s for s in corpus["sections"] if s["heading"].startswith("Appendix"))
    assert "separate rows for the same change" not in led["body"] and "counted once, through the PR" in led["body"]


def _sk(title, paths=None, ty="commit"):
    return {k["skill"] for k in map_skills([Item(id=title, source="github", type=ty, date="2026-08-01", title=title, text=title, meta={"mine": True, "paths": paths or []})])}


def test_skill_evidence_is_skill_specific_not_an_extension_blanket():
    assert "frontend" not in _sk("feat: add QUICKSTART guide for manual backend and frontend")
    assert "frontend" not in _sk("Update frontend styling docs", ["docs/frontend.md"])
    assert "frontend" not in _sk("Add frontend screenshot helper script")  # tooling title, no files: not UI implementation
    assert "frontend" not in _sk("Add frontend checks to CI", [".github/workflows/ci.yml"])  # CI yaml does not imply frontend
    assert "frontend" in _sk("Add frontend screenshot helper script", ["src/components/Shot.tsx"])  # the files show UI source
    assert "frontend" in _sk("feat: integrate Tailwind CSS and refactor styles", ["src/App.css", "package.json"])
    assert "frontend" in _sk("feat: Add initial frontend setup with App component")
    assert "accessibility" in _sk("Add aria-label to buttons", ["src/Button.tsx"])
    assert "accessibility" not in _sk("Add aria-label guide", ["docs/aria.md"])
    # relevant engineering config can still evidence its own skill
    assert "async messaging (queues)" in _sk("Add dead letter queue for webhooks", ["deploy/queues.yml", "docs/queues.md"])
    assert "async messaging (queues)" not in _sk("Document the webhook queue", ["docs/queues.md"])


def test_design_skill_survives_the_docs_filter_but_gitignore_docs_do_not():
    assert "technical design" in _sk("Add ADR 0004: choose Kafka over SQS", ["docs/adr/0004-kafka.md"])
    assert "technical design" in _sk("RFC 12: retry strategy for payouts", ["docs/rfcs/rfc-12-retries.md"])
    assert "technical design" in _sk("Add design doc for search ranking", ["docs/design/search-ranking.md"], ty="pr")
    assert "technical design" not in _sk("Keep session design docs local-only via gitignore", [".gitignore"])
    assert "technical design" not in _sk("Remove CLAUDE.md from .gitignore to allow tracking of design document", [".gitignore"])


def test_doc_type_items_are_not_bypassing_the_pr_commit_filter():
    doc = Item(id="d", source="doc", type="doc", date="2026-09-02", title="Design: Search ranking approach", text="design doc for ranking decision", meta={"mine": True})
    ci = Item(id="c", source="github", type="commit", date="2026-09-03", title="docs: frontend guide", text="", meta={"mine": True, "paths": ["docs/frontend.md"]})
    got = {k["skill"]: k["evidence"] for k in map_skills([doc, ci])}
    assert got["technical design"] == ["d"] and "frontend" not in got  # the doc counts for design; the docs commit never reaches frontend


def test_regression_test_is_supporting_work_not_a_second_solved_problem():
    fix = citem("e742425aaaa", E742425)
    test = citem("7315c86bbbb", "Add regression test for the rebuild turn-count double-charge\n\nConfirms rebuildAndRedeploy() now adds exactly one transcript entry\nfor a successful build+restart. Verified this test actually catches the bug it targets: "
                                "reverted the prior change locally and the test failed with 'expected: <1> but was: <2>'.")
    items = [fix, test]
    corpus, _ = corpus_for("backfill", profile(items), items, title="t", terms=set(), keep_internal=False, colleagues=[], names=NAMES,
                           date_from="", date_to="", run_notes=[], failed=[], quiet_line=None, new_ids=set())
    body = next(s for s in corpus["sections"] if s["heading"] == "Problems solved")["body"]
    assert body.count("Commit message") == 0 and body.count("Problem stated in the commit message") == 1
    assert "Regression test: Add regression test" in body and "7315c86bbbb" in body and "e742425aaaa" in body  # one problem, both cited
    assert "not independently verified" in body
