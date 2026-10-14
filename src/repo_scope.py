"""Default GitHub scope: owned/member-org repos with authored commits in the window.
Gateway argument shapes were read from the live /tools catalog; no private account
content is accessed by the offline tests.
"""
from __future__ import annotations
import asyncio
from adapters import rows, failed_payload

MAX_PAGES = 500


async def pages(call, name, args, keys):
    result, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        response = await call(name, {**args, "page": page, "per_page": 100})
        if bad := failed_payload(response):
            raise RuntimeError(bad)
        batch = rows(response, *keys)
        if not batch:
            return result
        fresh = []
        for r in batch:
            key = str(r.get("id") or r.get("full_name") or r.get("login") or r.get("name") or r.get("sha") or "")
            if not key:
                raise RuntimeError("listing row has no stable identity")
            if key not in seen:
                seen.add(key)
                fresh.append(r)
        if not fresh:
            raise RuntimeError("listing repeated a page; completeness unverified")
        result.extend(fresh)
    raise RuntimeError("listing exceeded page limit; completeness unverified")


async def discover(*args, **kwargs):
    """Compatibility guard: account/org discovery is intentionally unavailable."""
    raise ValueError("Automatic repository discovery is disabled. Select explicit owner/repo entries.")


def summary(scope):
    read = ", ".join(scope.get("read", [])) or "none"
    parts = [f"{len(scope.get('no_commits', []))} others skipped: no commits in the window"] if scope.get("mode") == "discovery" else ["explicit allow-list; discovery off"]
    if scope.get("excluded"):
        parts.append(f"{len(scope['excluded'])} excluded by you")
    if scope.get("unverified"):
        parts.append(f"{len(scope['unverified'])} could not be verified")
    return f"{'Selected saved-evidence repos' if scope.get('mode') == 'selected-saved-view' else 'Repos read'}: {read} ({'; '.join(parts)})."
