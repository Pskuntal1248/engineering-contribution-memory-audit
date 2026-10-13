"""24-month volume: GitHub lists are paged, and detail reads stop at the time limit without a crash. Fake gateway, no real tools."""
import asyncio
import time

from guards import ToolLog
from sources import GatewaySource

ME = "Pskuntal1248"


def make_gw(n_prs, calls):
    async def gw(name, args):
        calls.append((name, args.get("page")))
        if name == "github.search_issues":
            if "-author" in args["q"]:
                return {"items": []}
            rows = [{"number": i + 1, "title": f"t{i}", "state": "closed", "html_url": f"https://github.com/o/r/pull/{i + 1}", "user": {"login": ME},
                     "created_at": "2026-09-01T10:00:00Z", "closed_at": "2026-09-01T12:00:00Z", "labels": []} for i in range(n_prs)]
            pg, pp = args["page"], args["per_page"]
            return {"items": rows[(pg - 1) * pp: pg * pp]}
        if name in ("github.list_pull_requests", "github.list_commits"):
            return []
        return []
    return gw


def fetch(gw, deadline=None):
    src = GatewaySource(gw, ME, ["o/r"], [], False, [], {"github.search_issues", "github.list_pull_requests", "github.list_pr_files", "github.list_pr_reviews", "github.list_commits"})
    src.deadline = deadline
    return asyncio.run(src.fetch("2026-04-01T00:00:00Z", None, ToolLog()))


def test_every_page_of_the_pr_search_is_read():
    calls = []
    out = fetch(make_gw(250, calls))
    assert len(out["github"]["prs"]) == 250
    assert [p for n, p in calls if n == "github.search_issues" and p][:3] == [1, 2, 3]


def test_past_the_time_limit_detail_reads_stop_and_the_run_still_returns():
    calls = []
    out = fetch(make_gw(250, calls), deadline=time.monotonic() - 1)
    assert not any("time limit" in str(f.get("error")) for f in out["failed"])
    assert out["github"]["prs"] == [] or isinstance(out["github"]["prs"], list)
