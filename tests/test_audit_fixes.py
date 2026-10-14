"""Fixes from the UC-10 audit: explicit input errors, Drive body text, squash merge sha, redacted headings, framework message."""
import asyncio

import pytest

import adapters
import agent
from guards import ToolLog, parse_channels
from inputs import validate_inputs
from pipeline import covered_by_pr


def test_valid_inputs_pass():
    ok = {"backfill_months": 6, "request": "one_on_one", "docs_source": "Notion + Google Drive", "period_from": "2026-04-01", "period_to": "2026-09-30",
          "repos_or_org": "o/r, o/s", "timezone": "Asia/Kolkata", "team_changes": "2026-07-01: Platform", "include_dms": "No"}
    assert validate_inputs(ok) == []
    assert validate_inputs({}) == []
    for d in ["None", "Confluence", "Notion", "Google Drive", "Confluence + Notion", "Confluence + Google Drive", "Notion + Google Drive", "All three", "all three"]:
        assert validate_inputs({"docs_source": d}) == [], d


@pytest.mark.parametrize("bad,key", [
    ({"backfill_months": "abc"}, "backfill_months"), ({"backfill_months": "6.5"}, "backfill_months"), ({"backfill_months": 0}, "backfill_months"),
    ({"backfill_months": -3}, "backfill_months"), ({"backfill_months": 6.5}, "backfill_months"), ({"backfill_months": "0"}, "backfill_months"),
    ({"request": "foo"}, "request"), ({"docs_source": "Dropbox"}, "docs_source"), ({"docs_source": "None + Notion"}, "docs_source"),
    ({"period_from": "2026-13-45"}, "period_from"), ({"period_to": "last spring"}, "period_to"), ({"period_from": "01/04/2026"}, "period_from"),
    ({"period_from": "2026-09-30", "period_to": "2026-04-01"}, "after period_to"), ({"team_changes": "2026-13-45: X"}, "team_changes"),
    ({"team_changes": "July: Platform"}, "team_changes"), ({"repos_or_org": "https://github.com/o/r"}, "repos_or_org"), ({"repos_or_org": "o/r;o/s"}, "repos_or_org"),
    ({"jira_projects": "PAY;SRCH"}, "jira_projects"), ({"slack_channels": "#a;#b"}, "slack_channels"), ({"timezone": "Mars/Base"}, "timezone"), ({"timezone": "asia/kolkata"}, "timezone"), ({"timezone": "IST"}, "timezone"),
    ({"include_dms": "maybe"}, "include_dms"),
])
def test_bad_inputs_are_named(bad, key):
    errs = validate_inputs(bad)
    assert errs and any(key in e for e in errs), errs


def test_all_errors_reported_in_one_pass_and_nothing_is_read():
    calls = []

    class Src:
        async def fetch(self, *a):
            calls.append("fetch")
            return {}
    deps = agent.Deps(lambda cfg: Src(), llm=None)
    with pytest.raises(Exception) as ei:
        asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {"github_username": "u", "repos_or_org": "o/r", "backfill_months": "abc", "request": "foo", "period_from": "x"}, "memory": {}}))
    msg = str(ei.value)
    assert "backfill_months" in msg and "request" in msg and "period_from" in msg and not calls


def test_drive_body_is_the_document_text_not_a_download_link():
    doc = {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "PAY-101: chose idempotent retries.\n"}}, {"textRun": {"content": "Second line"}}]}}]}, "title": "T",
           "documentStyle": {"x": "LEFT_TO_RIGHT"}}
    assert adapters.docs_text(doc) == "PAY-101: chose idempotent retries. Second line"
    assert adapters.docs_text({"data": {"display_url": "https://drive.google.com/x", "file": {"s3url": "https://r2/x"}}}) == ""


def test_squash_commit_is_covered_by_its_prs_merge_sha():
    import normalize as N
    pr = {"_repo": "o/r", "number": 12, "title": "Add retry", "user": {"login": "u"}, "created_at": "2026-10-01T10:00:00Z", "merged_at": "2026-10-02T10:00:00Z", "state": "closed",
          "merge_commit_sha": "s" * 40, "_commit_shas": ["1" * 40], "files": []}
    raw = {"sha": "s" * 40, "author": {"login": "u"}, "commit": {"message": "Rewritten title", "author": {"name": "u", "date": "2026-10-02T10:00:00Z"}}, "html_url": "x", "parents": [{}]}
    c = N.normalize_commit(raw, "o/r", "u", [])
    assert covered_by_pr(c, [pr]) is True


def test_channel_names_have_no_stray_space():
    assert parse_channels(" # a , #b") == ["a", "b"]
    assert parse_channels(" # ") == []


def _run(inputs, source=None, memory=None):
    class Src:
        async def fetch(self, *a):
            return {"github": {"prs": [], "reviews": {}, "commits": []}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": [], "failed": [], "capped": []}
    deps = agent.Deps(lambda cfg: source or Src(), llm=None)
    return asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {"github_username": "u", "repos_or_org": "o/r", "role_and_level": "SE", **inputs}, "memory": memory or {}}))


def test_skipped_slack_is_said_out_loud():
    out = _run({"slack_channels": ""})
    assert "no channels selected" in out["corpus"]["summary"]


def test_doc_keys_only_from_configured_jira_projects():
    import normalize as N
    d = {"id": "1", "title": "UC-10 design", "body": "PAY-101 and AUTH-7 and FOO-1", "url": "https://x/1", "_mine": True}
    assert N.normalize_doc(d, "drive", [], ["PAY", "AUTH"]).keys == ["PAY-101", "AUTH-7"]


def test_no_repos_is_not_a_cap_note():
    from sources import GatewaySource
    async def call(n, a):
        return {}
    out = {"github": {"prs": [], "reviews": {}, "commits": []}, "failed": [], "capped": []}
    asyncio.run(GatewaySource(call, "u", [], [], False, [], set())._github(out, "2026-01-01T00:00:00Z", ToolLog()))
    assert not out["capped"]


def test_ledger_file_is_rewritten_even_when_empty(tmp_path):
    import outputs
    (tmp_path / "last_ledger.csv").write_text("stale\n")
    outputs.write_local_copy({"sections": []}, str(tmp_path))
    assert (tmp_path / "last_ledger.csv").read_text().startswith("source,date")
