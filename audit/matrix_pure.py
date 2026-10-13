"""Audit matrix, pure-parser level (no network, no model). Prints observed behaviour per input value."""
import sys
sys.path.insert(0, "src")
import agent
from ask import parse_request, parse_team_changes
from guards import parse_channels
from models import local_tz
from datetime import date

def show(title, rows):
    print(f"\n## {title}")
    for k, v in rows:
        print(f"  {k!r:55} -> {v!r}")

show("docs_source -> parsed sources (8 manifest options)", [(o, agent._docs_sources(o)) for o in
     ["None","Confluence","Notion","Google Drive","Confluence + Notion","Confluence + Google Drive","Notion + Google Drive","All three"]])
show("docs_source variants / malformed", [(o, agent._docs_sources(o)) for o in
     ["", "none", "NONE", "all three", "All Three ", "notion+confluence", "Google Drive+Notion", "Dropbox", "Notion, Dropbox", "Notion + Notion", "Drive", "gdrive", "All"]])

def cfg(**kw):
    base = {"github_username": "u", "repos_or_org": "o/r", "jira_projects": "PAY", "slack_channels": "general", "role_and_level": "SE"}
    base.update(kw); return agent._cfg(base)
show("repos_or_org", [(v, cfg(repos_or_org=v).repos) for v in
     ["o/r", "o/r, o/s", "o/r\no/s", "o/r;o/s", "o/r o/s", "https://github.com/o/r", "O/R", "myorg", "o/r,", " o/r ", ""]])
show("jira_projects", [(v, cfg(jira_projects=v).jira_projects) for v in
     ["PAY, SRCH, AUTH", "pay,srch", "PAY\nSRCH", "PAY;SRCH", "PAY SRCH", "PAY-1", "pay, ,srch", ""]])
show("slack_channels", [(v, cfg(slack_channels=v).slack_channels) for v in
     ["#uc10-test", "uc10-test", "#a, #b", "#a\n#b", "#a;#b", "C0C7U7L87MM", "#A", "##a", " # a ", ""]])
show("include_dms", [(v, cfg(include_dms=v).include_dms) for v in ["No", "Yes", "yes", "YES", "true", "True", "Y", "1", "", "maybe", None]])
show("slack_user_ids", [(v, cfg(slack_user_ids=v).slack_user_ids) for v in ["U123", "U1, U2", "u1", "", "U1;U2"]])
show("display_name / username -> name_hints", [((u, d), cfg(github_username=u, display_name=d).name_hints) for u, d in
     [("Pskuntal1248", "Parth Singh"), ("Pskuntal1248", ""), ("", "Parth Singh"), ("Pskuntal1248", "Parth Singh, P Singh")]])
show("timezone (local_tz)", [(v, str(local_tz(v))) for v in ["Asia/Kolkata", "UTC", "America/New_York", "Mars/Base", "", None, "IST", "asia/kolkata"]])
show("parse_request (ask) mode,from,to", [(v, parse_request(v, date(2026,10,13))) for v in
     ["1:1 points", "interview stories", "evidence gaps", "weekly summary", "review material April to September",
      "review material 2026-04-01 to 2026-09-30", "review 2026-09-30 2026-04-01", "promotion evidence", "what did I do on project X", "year summary", "gibberish", "", "review material 2026-13-45 to 2026-14-99"]])
show("parse_team_changes", [(v, parse_team_changes(v)) for v in
     ["2026-07-01: Platform", "2026-07-01: Platform\n2026-03-01: Payments", "2026-07-01 - Platform", "July 1: Platform", "2026-13-45: Bad", "Platform", "2026-07-01:", ""]])
