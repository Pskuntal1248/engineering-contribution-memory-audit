"""Step 17 (MOCK): DM / private-channel gating at the real GatewaySource boundary. Records every Slack tool call + its channel."""
import sys, asyncio, json; sys.path.insert(0, "src")
from datetime import datetime, timezone
import agent
from sources import GatewaySource
HAS = {"slack.list_channels","slack.search_messages","slack.fetch_message_thread_from_a_conversation","slack.read_history","github.search_issues","github.list_commits","jira.search_issues","jira.get_all_projects"}
CH = [{"id": "C1", "name": "uc10-test", "is_member": True}, {"id": "C2", "name": "general", "is_member": True},
      {"id": "G1", "name": "secret-private", "is_private": True, "is_member": True},
      {"id": "D1", "name": "", "is_im": True, "user": "U9"}, {"id": "M1", "name": "mpdm-a-b", "is_mpim": True}]
def run(chans_input, dms):
    seen = []
    async def call(name, args):
        if name.startswith("slack."):
            seen.append((name, args.get("channel") or args.get("query") or args.get("types") or "", ))
        if name == "slack.list_channels": return {"channels": CH if "im" in str(args.get("types")) else [c for c in CH if not c.get("is_im") and not c.get("is_mpim")]}
        if name == "slack.read_history": return {"messages": [{"ts": "1791000000.0", "user": "U9", "text": "private dm text"}]}
        if name == "slack.search_messages": return {"messages": {"matches": []}}
        if name == "jira.get_all_projects": return {"values": [{"key": "PAY"}]}
        if name == "github.search_issues": return {"items": []}
        return []
    deps = agent.Deps(lambda cfg: GatewaySource(call, cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, HAS, [], cfg.name_hints),
                      llm=None, now=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {"github_username": "u", "repos_or_org": "o/r", "jira_site": "https://x.atlassian.net", "jira_projects": "PAY",
         "slack_channels": chans_input, "include_dms": dms, "role_and_level": "SE"}, "memory": {}}))
    r = out["result"]
    return seen, r.get("dm_reads"), r.get("status"), [n[:90] for n in r.get("notes", []) if "dm" in n.lower() or "DM" in n]
for label, chans, dms in [("public only, DMs=No", "#uc10-test", "No"), ("DM id listed but DMs=No", "#uc10-test, U9", "No"), ("*dm* but DMs=No", "#uc10-test, *dm*", "No"),
                          ("private channel explicitly listed, DMs=No", "#uc10-test, secret-private", "No"), ("private NOT listed, DMs=No", "#uc10-test", "No"),
                          ("DMs=Yes, only public listed", "#uc10-test", "Yes"), ("DMs=Yes + DM user id listed", "#uc10-test, U9", "Yes"), ("DMs=Yes + *dm*", "#uc10-test, *dm*", "Yes")]:
    seen, dmr, status, notes = run(chans, dms)
    types = [s[1] for s in seen if s[0] == "slack.list_channels"]
    hist = [s[1] for s in seen if s[0] == "slack.read_history"]
    srch = [s[1] for s in seen if s[0] == "slack.search_messages"]
    print(f"- {label:44} status={status} list_channels.types={types} search={srch} read_history={hist} dm_reads={dmr} {notes}")
