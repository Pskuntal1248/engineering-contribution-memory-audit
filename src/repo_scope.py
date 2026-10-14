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


async def discover(call, username, since, until, excluded, log, metadata=None):
    async def invoke(name, args):
        log.add(name, **args)
        return await call(name, args)
    # Do not use repository 'updated' timestamps as a proxy for user commits.
    owned = await pages(invoke, "github.list_repositories_for_the_authenticated_user", {"type": "owner"}, ("repositories", "repos"))
    orgs = await pages(invoke, "github.list_organizations_for_the_authenticated_user", {}, ("organizations",))
    candidates = list(owned)
    for org in orgs:
        login = str(org.get("login") or "")
        if not login:
            raise RuntimeError("organization membership could not be verified")
        candidates.extend(await pages(invoke, "github.list_organization_repositories", {"org": login, "type": "all"}, ("repositories", "repos")))
    by_name = {}
    for r in candidates:
        full = str(r.get("full_name") or "")
        if "/" not in full:
            raise RuntimeError("repository full name could not be verified")
        by_name.setdefault(full.lower(), full)
        if metadata is not None:
            metadata[full.lower()] = r
    excluded = {r.lower() for r in excluded}
    names = [v for k, v in by_name.items() if k not in excluded]
    selected, no_commits, failures = [], [], []
    gate = asyncio.Semaphore(6)
    async def qualify(repo):
        owner, name = repo.split("/", 1)
        async with gate:
            try:
                # One result is enough to prove eligibility, including forks. Empty is
                # valid only for a successful response; errors are never "no commits".
                args = {"owner": owner, "repo": name, "author": username, "since": since, "until": until, "per_page": 100, "page": 1}
                response = await invoke("github.list_commits", args)
                if bad := failed_payload(response):
                    raise RuntimeError(bad)
                commits = rows(response, "commits")
                if commits:
                    selected.append(repo)
                    return
                branches = await pages(invoke, "github.list_branches", {"owner": owner, "repo": name}, ("branches",))
                for branch in branches:
                    ref = str(branch.get("name") or "")
                    if not ref:
                        raise RuntimeError("branch name could not be verified")
                    response = await invoke("github.list_commits", {**args, "sha": ref})
                    if bad := failed_payload(response):
                        raise RuntimeError(bad)
                    if rows(response, "commits"):
                        selected.append(repo)
                        return
                no_commits.append(repo)
            except Exception as e:
                failures.append({"source": f"github-discovery:{repo}", "error": "commit eligibility could not be verified: " + str(e)[:160]})
    await asyncio.gather(*(qualify(r) for r in names))
    return sorted(selected), {"mode": "discovery", "read": sorted(selected), "no_commits": sorted(no_commits), "excluded": sorted(v for k, v in by_name.items() if k in excluded), "unverified": [f["source"].split(":", 1)[1] for f in failures]}, failures


def summary(scope):
    read = ", ".join(scope.get("read", [])) or "none"
    parts = [f"{len(scope.get('no_commits', []))} others skipped: no commits in the window"] if scope.get("mode") == "discovery" else ["explicit allow-list; discovery off"]
    if scope.get("excluded"):
        parts.append(f"{len(scope['excluded'])} excluded by you")
    if scope.get("unverified"):
        parts.append(f"{len(scope['unverified'])} could not be verified")
    return f"Repos read: {read} ({'; '.join(parts)})."
