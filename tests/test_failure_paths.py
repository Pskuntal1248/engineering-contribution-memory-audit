"""Failure paths seen in a real weekly run: a repo that could not be read must never read as 'nothing new'. Local logic with fixture sources only."""
import sys, asyncio
sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from datetime import datetime, timedelta, timezone
import agent
from guards import github_error
from report_notes import coverage
from weekly import quiet_week_line

API = Exception('{"message":"Validation Failed","errors":[{"message":"The listed users and repositories cannot be searched either because the resources do not exist or you do not have permission to view them.","resource":"Search"}]}')


def test_github_lookup_error_is_a_sentence_not_cut_off_json():
    t = github_error("pskuntal124/agent-testing", API)
    assert "pskuntal124/agent-testing" in t and "owner/repo spelling" in t and "{" not in t and "Validation" not in t


def test_quiet_line_never_claims_nothing_happened_when_a_repo_failed():
    t = quiet_week_line("2026-08-21", "2026-10-20", ["o/r"])
    assert t.startswith("Could not check o/r") and "NOT a finding" in t
    assert "No new work found" not in t


def test_long_window_says_its_length():
    assert "60 days" in quiet_week_line("2026-08-21", "2026-10-20")
    assert "days" not in quiet_week_line("2026-10-13", "2026-10-20")


def test_saved_items_do_not_turn_a_total_failure_into_partly_read():
    f = [{"source": "github:o/r", "error": "x"}]
    line, st = coverage({"github": True}, f, {"github": 5}, read_now={"github": False})
    assert st["github"] == "could not be read" and "partly read" not in line and "earlier saved GitHub work" in line
    line, st = coverage({"github": True}, f, {"github": 5}, read_now={"github": True})
    assert st["github"] == "partly read"


class Failing:
    def __init__(self, ok):
        self.ok = ok

    async def fetch(self, since, until, log):
        from test_fixes3 import pr_dict, raw
        if not self.ok:
            return {**raw([], []), "failed": [{"source": "github:bad/repo", "error": github_error("bad/repo", API)}], "capped": []}
        return {**raw([pr_dict(1, "Add payout retries", created="2026-10-01T10:00:00Z", merged="2026-10-01T12:00:00Z")], []), "failed": [], "capped": []}


def test_weekly_run_with_unreadable_repo_is_not_reported_as_quiet():
    inp = {"github_username": "Pskuntal1248", "repos_or_org": "o/r", "role_and_level": "Software Engineer", "request": "weekly_update"}
    d1 = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)
    a = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: Failing(True), llm=None, now=lambda: d1)).ainvoke({"inputs": inp, "memory": {}}))
    b = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: Failing(False), llm=None, now=lambda: d1 + timedelta(days=7))).ainvoke({"inputs": inp, "memory": a["memory"]}))
    text = str(b["corpus"])
    assert "Nothing new this week" not in text and "No new work found" not in text
    assert "Could not check" in text and "partly read" not in text and "{\"message\"" not in text
    assert b["result"]["coverage"]["github"] == "could not be read"


class Spy:
    def __init__(self):
        self.since = []

    async def fetch(self, since, until, log):
        from test_fixes3 import raw
        self.since.append(since)
        return {**raw([], []), "failed": [], "capped": []}


def test_weekly_window_is_the_last_7_days_not_back_to_the_last_saved_item():
    inp = {"github_username": "Pskuntal1248", "repos_or_org": "o/r", "role_and_level": "Software Engineer", "request": "weekly_update"}
    from test_failure_paths import Failing
    d1 = datetime(2026, 8, 22, 9, tzinfo=timezone.utc)
    a = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: Failing(True), llm=None, now=lambda: d1)).ainvoke({"inputs": inp, "memory": {}}))
    spy = Spy()
    d2 = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
    b = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: spy, llm=None, now=lambda: d2)).ainvoke({"inputs": inp, "memory": a["memory"]}))
    assert b["result"]["mode"] == "weekly"
    assert spy.since[0][:10] <= "2026-09-30"  # the read still starts at the saved cursor (nothing from a missed week is lost); only the report is cut to 7 days
    assert "2026-10-13 and 2026-10-20" in str(b["corpus"]) and "days, from" not in str(b["corpus"])


def test_no_jira_keys_means_no_jira_site_lookup():
    calls = []

    async def site():
        calls.append(1)
        return "https://x.atlassian.net"
    inp = {"github_username": "Pskuntal1248", "repos_or_org": "o/r", "role_and_level": "Software Engineer", "request": "weekly_update"}
    d = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
    r = asyncio.run(agent.make_graph(agent.Deps(lambda cfg: Spy(), llm=None, now=lambda: d, site=site)).ainvoke({"inputs": inp, "memory": {}}))
    assert not calls and "Jira site taken" not in str(r["corpus"])
    asyncio.run(agent.make_graph(agent.Deps(lambda cfg: Spy(), llm=None, now=lambda: d, site=site)).ainvoke({"inputs": {**inp, "jira_projects": "ABC"}, "memory": {}}))
    assert calls
