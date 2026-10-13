"""Run 6 step 15 (MOCK at the adapter boundary - nothing is written to Drive). Drive gating negatives that live data does not have."""
import sys, asyncio; sys.path.insert(0, "src")
import adapters
from guards import ToolLog

ME = {"displayName": "Parth Singh", "emailAddress": "me@example.com"}
OTHER = {"displayName": "Sam Lee", "emailAddress": "sam@example.com"}
NAMESAKE = {"displayName": "Parth Singh", "emailAddress": "other-parth@example.com"}
SINCE = "2026-04-13T00:00:00Z"

def f(i, name, last, mod="2026-10-01T10:00:00Z"):
    return {"id": i, "name": name, "mimeType": "application/vnd.google-apps.document", "modifiedTime": mod, "createdTime": "2026-01-01T00:00:00Z",
            "webViewLink": f"https://docs.google.com/document/d/{i}/edit", "owners": [ME], "lastModifyingUser": last}
FILES = [f("A", "A edited by me in period", ME), f("B", "B owned, edited by someone else", OTHER), f("C", "C my revision BEFORE period", OTHER),
         f("D", "D my revision IN period", OTHER), f("E", "E revisions unreadable", OTHER), f("F", "F revisions empty", OTHER),
         f("H", "H namesake (same name, other email)", NAMESAKE), f("I", "I last modifier has no email", {"displayName": "Parth Singh"})]
REVS = {"B": [{"modifiedTime": "2026-09-01T00:00:00Z", "lastModifyingUser": OTHER}],
        "C": [{"modifiedTime": "2025-12-01T00:00:00Z", "lastModifyingUser": ME}, {"modifiedTime": "2026-09-01T00:00:00Z", "lastModifyingUser": OTHER}],
        "D": [{"modifiedTime": "2026-08-01T00:00:00Z", "lastModifyingUser": ME}],
        "F": [], "H": [{"modifiedTime": "2026-09-01T00:00:00Z", "lastModifyingUser": NAMESAKE}], "I": [{"modifiedTime": "2026-09-01T00:00:00Z", "lastModifyingUser": {"displayName": "Parth Singh"}}]}

async def run(about_fails=False):
    calls, out = [], {"docs": [], "failed": [], "capped": []}
    async def call(name, args):
        calls.append((name, args.get("fileId")))
        if name == "drive.get_about":
            if about_fails: raise RuntimeError("401 Unauthorized: missing scope")
            return {"user": ME}
        if name == "drive.find_file": return {"files": FILES}
        if name == "drive.list_revisions":
            if args["fileId"] == "E": raise RuntimeError("403 insufficient permissions for revisions")
            return {"revisions": REVS.get(args["fileId"], [])}
        if name == "drive.export_google_workspace_file": return {"data": {"content": f"body of {args['fileId']} PAY-1"}}
        raise RuntimeError("unexpected " + name)
    await adapters.fetch_drive(call, SINCE, ToolLog(), out)
    return calls, out

calls, out = asyncio.run(run())
exports = {a for n, a in calls if n == "drive.export_google_workspace_file"}
print("export (body read) calls for:", sorted(exports))
for d in sorted(out["docs"], key=lambda d: d["id"]):
    print(f"  {d['id']} credited={d['_mine']!s:5} body_read={bool(d['body'])!s:5} {d['title']}")
print("capped:", out["capped"]); print("failed:", out["failed"])
calls, out = asyncio.run(run(about_fails=True))
print("\n[get_about fails] docs:", len(out["docs"]), "exports:", len([c for c in calls if c[0].endswith('export_google_workspace_file')]), "failed:", [x["error"][:90] for x in out["failed"]])
