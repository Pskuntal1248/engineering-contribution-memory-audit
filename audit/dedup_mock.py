"""Run 4 supplement (MOCK, not live): squash-merge residual classes against the real covered_by_pr/ingest code."""
import sys; sys.path.insert(0, "src")
from datetime import datetime, timezone
import pipeline, normalize as N
from pipeline import Config

ME = "u"
def pr(num, title, files, shas, merged="2026-10-02T10:00:00Z", msha=""):
    return {"_repo": "o/r", "number": num, "title": title, "body": "", "user": {"login": ME}, "created_at": "2026-10-01T10:00:00Z", "closed_at": merged,
            "merged_at": merged, "merged": True, "state": "closed", "html_url": f"https://github.com/o/r/pull/{num}", "merge_commit_sha": msha,
            "head": {"ref": "feature/x", "sha": shas[-1]}, "files": [{"filename": f, "additions": 5, "deletions": 1} for f in files], "_commit_shas": shas, "labels": []}
def commit(sha, msg, files, date="2026-10-02T10:00:00Z", parents=1):
    c = {"sha": sha, "html_url": f"https://github.com/o/r/commit/{sha}", "author": {"login": ME}, "_repo": "o/r", "parents": [{}] * parents,
         "commit": {"message": msg, "author": {"name": ME, "date": date}}}
    c["files"] = [{"filename": f, "additions": 5, "deletions": 1} for f in files] if files is not None else []
    return c

cases = {
  "A squash, title 'Add retry (#12)'":                  (pr(12, "Add retry", ["a.py", "b.py"], ["1"*40]), commit("s"*40, "Add retry (#12)", ["a.py", "b.py"])),
  "B squash, rewritten title, files subset of PR":       (pr(12, "Add retry", ["a.py", "b.py"], ["1"*40]), commit("s"*40, "Retry failed calls with backoff", ["a.py", "b.py"])),
  "C squash, rewritten title, commit files unavailable": (pr(12, "Add retry", ["a.py", "b.py"], ["1"*40]), commit("s"*40, "Retry failed calls with backoff", None)),
  "D squash, rewritten title, files superset of PR":     (pr(12, "Add retry", ["a.py", "b.py"], ["1"*40]), commit("s"*40, "Retry failed calls with backoff", ["a.py", "b.py", "c.py"])),
  "E squash, rewritten title, PR file list unavailable": (pr(12, "Add retry", [], ["1"*40]), commit("s"*40, "Retry failed calls with backoff", ["a.py"])),
  "F squash == PR.merge_commit_sha, nothing else shared": (pr(12, "Add retry", ["x.py"], ["1"*40], msha="s"*40), commit("s"*40, "Retry failed calls with backoff", ["z.py"])),
}
for label, (p, c) in cases.items():
    cfg = Config(username=ME, repos=["o/r"], jira_site="https://x.atlassian.net", jira_projects=["PAY"], docs_sources=[], slack_channels=[], include_dms=False, slack_user_ids=set(), name_hints=[ME])
    raw = {"github": {"prs": [p], "reviews": {}, "commits": [c]}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": [], "failed": [], "capped": []}
    ing = pipeline.ingest(raw, cfg)
    commits = [i for i in ing.items if i.type == "commit"]
    print(f"{label:55} -> commit rows kept: {len(commits)}  {'(DOUBLE-COUNT RISK)' if commits else '(covered)'}")
