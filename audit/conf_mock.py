"""Step 18 (MOCK): where does the [CONFIDENTIAL source] flag appear, per output mode. Spec text not available to me; this shows observed behaviour."""
import sys, asyncio, json; sys.path.insert(0, "src")
from datetime import datetime, timezone
import agent
FLAG = "[CONFIDENTIAL source - check before sharing]"
def doc(i, title, body, labels=()):
    return {"_source": "drive", "id": i, "title": title, "labels": list(labels), "author": "Me", "editors": ["Me"], "created": "2026-10-01T00:00:00Z", "modified": "2026-10-02T00:00:00Z",
            "url": f"https://docs.google.com/document/d/{i}/edit", "kind": "google doc", "body": body, "_mine": True, "body_failed": False, "_attribution": "edit evidence"}
DOCS = [doc("t1", "Confidential: Payout incident review", "Root cause was a retry storm. PAY-1 fixed it."),
        doc("t2", "Design: Search ranking approach", "Internal only. BM25 with title weighting. SRCH-1"),
        doc("t3", "Design: Retry policy", "We decided exponential backoff. PAY-2. This is fine to share."),
        doc("t4", "Q3 notes", "Anything restricted about the NDA lives elsewhere. PAY-3")]
class Src:
    async def fetch(self, since, until, log):
        return {"github": {"prs": [], "reviews": {}, "commits": []}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": DOCS, "failed": [], "capped": []}
store = {}
def run(extra, mem=None):
    deps = agent.Deps(lambda cfg: Src(), llm=None, now=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))
    return asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {"github_username": "u", "repos_or_org": "o/r", "jira_site": "https://x.atlassian.net", "jira_projects": "PAY", "slack_channels": "", "role_and_level": "SE", "docs_source": "Google Drive", "display_name": "Me", **extra}, "memory": mem or {}}))
b = run({}); mem = b.get("memory")
def text(o): return json.dumps(o.get("corpus"), default=str)
print("backfill profile: flag count =", text(b).count(FLAG))
import normalize as N
for d in DOCS:
    it = N.normalize_doc(d, ["Me"], ["Me"])
    print("  normalize.confidential:", d["title"][:40].ljust(40), getattr(it, "meta", {}).get("confidential"))
for mode in ["weekly_summary", "one_on_one", "review_material", "interview_stories", "evidence_gaps"]:
    o = run({"request": mode, "review_framework": "Level:\n- Writes design docs\n- Debugs incidents"}, mem)
    t = text(o)
    print(f"{mode:18} flag occurrences={t.count(FLAG)}  mentions confidential doc title={('Payout incident review' in t)}")
