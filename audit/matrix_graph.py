"""Audit matrix, graph level with a MOCK gateway (no network, no model). Mock results are NOT live proof."""
import sys, asyncio, json
sys.path.insert(0, "src")
from datetime import datetime, timezone
import agent
from sources import GatewaySource

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
HAS = {"github.search_issues","github.list_pull_requests","github.list_pr_files","github.list_pr_reviews","github.list_commits","github.get_commit",
       "jira.search_issues","jira.get_all_projects","jira.get_issue","slack.list_channels","slack.search_messages","slack.fetch_message_thread_from_a_conversation","slack.read_history",
       "confluence.cql_search","notion.search_notion_page","notion.get_page_markdown","drive.find_file"}

def make(calls):
    async def call(name, args):
        calls.append(name)
        if name == "slack.list_channels":
            return {"channels": [{"id": "C1", "name": "uc10-test", "is_member": True}, {"id": "C2", "name": "general", "is_member": True}]}
        if name == "jira.get_all_projects":
            return {"values": [{"key": "PAY"}, {"key": "SRCH"}, {"key": "AUTH"}]}
        if name == "github.search_issues":
            return {"items": [], "total_count": 0}
        if name in ("github.list_commits", "github.list_pull_requests", "jira.search_issues", "slack.search_messages"):
            return []
        return {}
    return call

BASE = {"github_username": "u", "repos_or_org": "o/r", "jira_site": "https://x.atlassian.net", "jira_projects": "PAY", "slack_channels": "#uc10-test",
        "role_and_level": "SE", "timezone": "Asia/Kolkata"}

def run(extra, memory=None, identity=None):
    calls = []
    inp = {**BASE, **extra}
    deps = agent.Deps(lambda cfg: GatewaySource(make(calls), cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, HAS, cfg.docs_sources, cfg.name_hints),
                      llm=None, now=lambda: NOW, identity=identity, site=None)
    try:
        out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": inp, "memory": memory or {}}))
        return out, calls, None
    except Exception as e:
        return None, calls, f"{type(e).__name__}: {str(e)[:160]}"

def summarize(out):
    if out is None: return {}
    r = out.get("result") or {}
    return {"mode": r.get("mode"), "status": r.get("status"), "unavail": r.get("unavailable"), "reasons": [str(x)[:110] for x in (r.get("unavailable_reasons") or [])][:3],
            "notes": [str(n)[:110] for n in (r.get("notes") or [])][:3]}

def case(label, extra, **kw):
    out, calls, err = run(extra, **kw)
    ncalls = len([c for c in calls if c not in ("slack.list_channels", "jira.get_all_projects")])
    print(f"- {label:46} err={err!s:.150} calls={len(calls)} fetch={ncalls} {json.dumps(summarize(out), default=str)[:520]}")

print("## backfill_months")
for v in [6, "6", 0, -3, "abc", "6.5", 6.5, 1000, 24, "", None, 1]:
    case(f"backfill_months={v!r}", {"backfill_months": v})
print("## request value outside dropdown")
for v in ["weekly_update", "foo", "WEEKLY_SUMMARY", "", None]:
    case(f"request={v!r} (empty memory)", {"request": v})
print("## docs_source garbage")
for v in ["Dropbox", "None", ""]:
    case(f"docs_source={v!r}", {"docs_source": v})
print("## repos format")
for v in ["o/r;o/s", "https://github.com/o/r", "myorg", ""]:
    case(f"repos_or_org={v!r}", {"repos_or_org": v})
print("## required inputs blank")
for k in ["jira_projects", "slack_channels", "role_and_level"]:
    case(f"{k}=''", {k: ""})
print("## github_username blank, no identity source")
case("github_username='' identity=None", {"github_username": ""})
print("## include_dms / dm handling")
for v in ["No", "Yes", "maybe"]:
    case(f"include_dms={v!r}", {"include_dms": v})
print("## timezone invalid")
for v in ["Mars/Base", "IST", ""]:
    case(f"timezone={v!r}", {"timezone": v})
print("## bad jira/slack names (preflight)")
case("slack '#nope' + jira 'ZZZ'", {"slack_channels": "#nope", "jira_projects": "ZZZ"})
case("jira lowercase 'pay'", {"jira_projects": "pay"})
case("jira 'PAY-1' (issue key not project)", {"jira_projects": "PAY-1"})
case("slack by id 'C1'", {"slack_channels": "C1"})
case("slack separators '#uc10-test;#general'", {"slack_channels": "#uc10-test;#general"})
case("slack '#general, #uc10-test'", {"slack_channels": "#general, #uc10-test"})
