"""Source adapters behind one interface. The graph only sees `fetch()`; where the data
comes from (real gateway tools, or fixtures in tests) is swappable.

A source that fails or does not exist is named in `failed`, never silently empty."""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from typing import Any, Awaitable, Callable, Protocol

from guards import ToolLog, err_text

Call = Callable[[str, dict], Awaitable[Any]]  # call("github.list_pr_reviews", {...}) -> parsed payload

MAX_PRS_PER_REPO = 600
MAX_COMMITS_PER_REPO = 300
MAX_COMMIT_LOOKUPS = 100
PARALLEL = 10  # simultaneous gateway calls
PAGE = 100  # the largest page the GitHub tools give
MAX_LIST_PAGES = 10
MAX_OWNER_PAGES = 500  # a bare org/user listing past 50,000 repositories is refused, not cut
MAX_PR_FILE_LOOKUPS = 400  # file lists are one call per PR; bounded and reported


class Source(Protocol):
    async def fetch(self, since: str, until: str | None, log: ToolLog) -> dict[str, Any]: ...


def _rows(data: Any, *keys: str) -> list[dict]:
    """Gateway payloads are lists, or dicts that hold the list under one of a few names."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in (*keys, "items", "data", "results", "values"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                got = _rows(v, *keys)
                if got:
                    return got
    return []


class FixtureSource:
    """Serves a prebuilt raw bundle (tests, local demo), cut off at `before` (ISO) and `since` like a real source."""

    def __init__(self, raw: dict[str, Any], before: str | None = None):
        self.raw, self.before = raw, before

    async def fetch(self, since: str, until: str | None, log: ToolLog) -> dict[str, Any]:
        from datetime import datetime, timezone
        hi = self.before or "9999"
        lo = since

        def ok(ts: str) -> bool:
            return lo <= ts < hi

        def tsiso(x: str) -> str:
            return datetime.fromtimestamp(float(x), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        g = self.raw["github"]
        prs = [p for p in g["prs"] if ok(p["created_at"])]
        keys = {f"{p['_repo']}#{p['number']}" for p in prs}
        return {
            "github": {"prs": prs, "reviews": {k: v for k, v in g["reviews"].items() if k in keys}, "commits": []},
            "jira": [t for t in self.raw["jira"] if ok(t["fields"]["created"])],
            "slack": {"channels": self.raw["slack"]["channels"], "threads": [t for t in self.raw["slack"]["threads"] if ok(tsiso(t["messages"][0]["ts"]))]},
            "docs": [d for d in self.raw["docs"] if ok(d["modified"])], "failed": [], "capped": [],
        }


class FetchTimeUp(RuntimeError):
    pass


class GatewaySource:
    """Real tools through the Gravity gateway. Only certified read tools are used by default.
    NOTE: argument names are best guesses from the tool descriptions; they are unverified until a run
    against a connected account (see report). Every failure is captured per source."""

    def __init__(self, call: Call, username: str, repos: list[str], slack_channels: list[str], include_dms: bool,
                 jira_projects: list[str], has: set[str], docs_sources: list[str] | None = None, names: list[str] | None = None):
        self.names = names or []
        self.scope_settings = None
        self.docs_sources = [d.lower() for d in (docs_sources or [])]
        gate = asyncio.Semaphore(PARALLEL)  # one limit for every source running side by side

        self.deadline: float | None = None  # monotonic time after which no new gateway call starts (set by the graph)
        self.skipped = 0

        async def limited(tool: str, args: dict) -> Any:
            async with gate:
                if self.deadline is not None and time.monotonic() > self.deadline:
                    self.skipped += 1
                    raise FetchTimeUp("fetch time limit reached")  # waiting callers stop here instead of running past the run's time limit
                return await call(tool, args)
        self.call, self.username, self.repos = limited, username, repos
        self.slack_channels, self.include_dms, self.jira_projects, self.has = slack_channels, include_dms, jira_projects, has

    def configure_scope(self, exclude_repos, window_start, window_end):
        self.scope_settings = (exclude_repos, window_start, window_end)

    async def fetch(self, since: str, until: str | None, log: ToolLog) -> dict[str, Any]:
        out: dict[str, Any] = {"github": {"prs": [], "reviews": {}, "commits": []}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": [], "failed": [], "capped": [], "coverage": {"jira": "no projects selected", "slack": "no channels selected", "docs": "no docs source selected"}}
        import adapters

        if self.scope_settings:
            exclusions, window_start, window_end = self.scope_settings
            if self.repos:
                # Explicit names/org overrides retain their existing expansion, but
                # no default account discovery or commit filter is used.
                pass
            else:
                from repo_scope import discover
                try:
                    self.repos, out["repo_scope"], issues = await discover(self.call, self.username, window_start, window_end, exclusions, log)
                    out["failed"].extend(issues)
                except Exception as e:
                    out["failed"].append({"source": "github-discovery", "error": "repository scope could not be verified: " + err_text(e, 200)})
                    out["blocked"] = True
                    return out
        chans, picked, projects, verified = await self._preflight(out, log)
        if self.scope_settings and "repo_scope" not in out:
            excluded = {r.lower() for r in self.scope_settings[0]}
            omitted = [r for r in self.repos if r.lower() in excluded]
            self.repos = [r for r in self.repos if r.lower() not in excluded]
            out["repo_scope"] = {"mode": "allow-list", "read": list(self.repos), "excluded": omitted, "no_commits": [], "unverified": []}
        if not verified:  # check every named source BEFORE doing any work: one bad input stops the whole read
            out["failed"].append({"source": "preflight", "error": "one or more configured sources could not be verified, so no contribution data was read at all. Fix the inputs above and rerun."})
            out["blocked"] = True
            return out

        async def slack_part() -> None:
            if picked:
                await self._slack(out, since, log, picked)

        async def jira_part() -> None:
            if not projects:
                return
            if "jira.search_issues" in self.has:
                await adapters.fetch_jira(self.call, projects, since, log, out)
            else:
                out["coverage"]["jira"] = "isn't available in this run"

        async def doc_part(src_name: str, tool: str, fn: Any) -> None:
            if not any(src_name in d or (src_name == "google drive" and "drive" in d) for d in self.docs_sources):
                return
            if tool in self.has:
                try:  # one docs source failing never stops the others
                    await (fn(self.call, since, log, out, self.names) if src_name in {"notion", "confluence"} else fn(self.call, since, log, out))
                except Exception as e:  # noqa: BLE001
                    out["failed"].append({"source": f"docs:{src_name}", "error": adapters.explain(f"docs:{src_name}", err_text(e, 400))})
            else:
                out["failed"].append({"source": f"docs:{src_name}", "error": f"{tool} is not available in this run"})
        # GitHub, Slack, Jira and the docs sources do not depend on each other: they run side by side (each with its own bounded pool)
        await asyncio.gather(self._github(out, since, log), slack_part(), jira_part(),
                             doc_part("confluence", "confluence.cql_search", adapters.fetch_confluence),
                             doc_part("notion", "notion.search_notion_page", adapters.fetch_notion),
                             doc_part("google drive", "drive.find_file", adapters.fetch_drive))
        if self.skipped:
            out["failed"] = [f for f in out["failed"] if "fetch time limit" not in str(f.get("error"))]
            out["capped"].append(f"time limit: stopped reading sources early, {self.skipped} detail reads (PR files, reviews, ticket history, Slack threads, pages) were not done; "
                                 "the items found by the list reads are still included. Narrow backfill_months or run again to cover the rest")
        if out.get("repo_scope"):
            broken = {f["source"].split(":", 1)[1] for f in out["failed"] if str(f["source"]).startswith("github:")}
            scope = out["repo_scope"]
            scope["read"] = [r for r in scope["read"] if r not in broken]
            scope["unverified"] = sorted(set(scope.get("unverified", [])) | broken)
        from coverage import unavailable
        selections = {"jira": bool(projects), "slack": bool(picked), "docs": bool(self.docs_sources)}
        for source, selected in selections.items():
            if not selected:
                continue
            errors = [f for f in out["failed"] if str(f.get("source", "")).startswith(source)]
            if errors and all(unavailable(f.get("error", "")) for f in errors):
                out["failed"] = [f for f in out["failed"] if f not in errors]
                out["coverage"][source] = "isn't connected or available"
            elif errors:
                out["coverage"][source] = "some selected reads failed"
            elif "isn't" not in out["coverage"].get(source, ""):
                out["coverage"][source] = "covered"
        return out

    async def _preflight(self, out: dict, log: ToolLog) -> tuple[list[dict], list[dict], list[str], bool]:
        """Verify every named source input BEFORE any contribution is read. The channel list and the project list are
        discovery calls, everything else waits. One unverifiable input fails the whole run's reads; the errors list what
        the connected account can actually see, with close-match suggestions - nothing is silently substituted."""
        from guards import channels_to_read, is_dm_channel
        import adapters
        chans: list[dict] = []
        picked: list[dict] = []
        projects = list(self.jira_projects)
        ok = True
        if self.repos:  # a bare organisation or user name becomes its repositories before anything is read; owner/repo entries are de-duplicated
            ok = await self._expand_repos(out, log)
        if self.slack_channels or self.include_dms:
            try:
                log.add("slack.list_channels")
                chans = await self._list_channels(log)
                out["slack"]["channels"] = chans
            except Exception as e:  # noqa: BLE001 - the channel list itself is unreadable: every channel input is unverified
                from coverage import unavailable
                if unavailable(str(e)):
                    out["coverage"]["slack"] = "isn't connected"
                else:
                    out["failed"].append({"source": "slack", "error": "the channel list could not be read, so the configured channels could not be verified: " + err_text(e, 200)})
                    ok = False
            else:
                picked, missing = channels_to_read(chans, self.slack_channels, self.include_dms, log)
                visible = sorted(str(c.get("name", "")) for c in chans if not is_dm_channel(c) and c.get("name"))
                for m in missing:
                    if not self.include_dms and (m == "*dm*" or re.fullmatch(r"[UWD](?=[A-Z0-9]*\d)[A-Z0-9]{6,}", m.upper())):
                        out["failed"].append({"source": f"slack:{m}", "error": f"{m} looks like a DM or a person's ID, but include_dms is No, so no DM is listed or read. Set include_dms to Yes to read it, or remove it from the channels box."})
                        continue
                    close = difflib.get_close_matches(m.lower(), [v.lower() for v in visible], n=3, cutoff=0.6)
                    shown = ", ".join("#" + v for v in visible[:40]) + (" ..." if len(visible) > 40 else "")
                    out["failed"].append({"source": f"slack:#{m}", "error": (
                        f"#{m} is not in the channel list of the connected Slack account. "
                        + (f"Did you mean: {', '.join('#' + c for c in close)}? " if close else "")
                        + f"Channels it can see: {shown}. "
                        "Rerun with the exact name or the channel ID. A private channel is only listed when the connected account is a member; "
                        "if the connection is older than the private channel, reconnect it so private channels are readable.")})
                for ch in picked:
                    if ch.get("is_member") is False:
                        missing.append(str(ch.get("name", "")))
                        out["failed"].append({"source": f"slack:#{ch.get('name', '')}", "error": "connected Slack account is not a member of this channel; join it, then rerun"})
                if missing:
                    ok = False
        if self.jira_projects:
            if "jira.get_all_projects" not in self.has:
                projects = []
                out["coverage"]["jira"] = "isn't available in this run"
            else:
                try:
                    log.add("jira.get_all_projects")
                    response = await self.call("jira.get_all_projects", {"max_results": 200})
                    if bad := adapters.failed_payload(response):
                        raise RuntimeError(bad)
                    seen = adapters.rows(response, "values", "projects", "results")
                except Exception as e:  # noqa: BLE001 - an auth refusal means the keys are unverified, not safe
                    from coverage import unavailable
                    if unavailable(str(e)):
                        projects = []
                        out["coverage"]["jira"] = "isn't connected"
                    else:
                        out["failed"].append({"source": "jira-preflight", "error": "the configured project keys could not be verified: " + err_text(e, 120)})
                        ok = False
                else:
                    canon = {str(p.get("key", "")).upper(): str(p.get("key", "")) for p in seen if p.get("key")}
                    canon = {k: v for k, v in canon.items() if k}
                    checked: list[str] = []
                    for proj in projects:
                        hit = canon.get(str(proj).upper())
                        if hit:
                            checked.append(hit)
                        else:
                            ok = False
                            close = difflib.get_close_matches(str(proj).upper(), sorted(canon), n=3, cutoff=0.6)
                            out["failed"].append({"source": f"jira:{proj}", "error": (
                                f"project {proj} is not visible to the connected Jira account. "
                                + (f"Did you mean: {', '.join(close)}? " if close else "")
                                + f"Projects it can see: {', '.join(sorted(canon.values())) or 'none'}. Rerun with the exact project key.")})
                    projects = checked
        return chans, picked, projects, ok

    async def _owner_repos(self, tool: str, args: dict, log: ToolLog) -> list[str]:
        """Every repository the tool lists for one owner: page after page (per_page is always set - the tools default to 1 for an organisation and 30 for a
        user), until a page is empty. A nonempty page that adds nothing new is the tool repeating itself, so the listing is incomplete and this raises:
        a list that could not be read to its end is never returned as if it were complete."""
        import adapters
        names: list[str] = []
        seen: set[str] = set()
        for page in range(1, MAX_OWNER_PAGES + 1):
            log.add(tool, page=page)
            resp = await self.call(tool, {**args, "per_page": PAGE, "page": page})
            bad = adapters.failed_payload(resp)
            if bad:
                raise RuntimeError(bad)
            got = _rows(resp, "repositories", "repos", "items", "data")
            if not got:
                return names  # an empty page ends the listing
            fresh = []
            for r in got:
                owner = (r.get("owner") or {}).get("login") if isinstance(r.get("owner"), dict) else None
                full = str(r.get("full_name") or (f"{owner}/{r.get('name')}" if owner and r.get("name") else ""))
                if "/" in full and full.lower() not in seen:
                    seen.add(full.lower())
                    fresh.append(full)
            if not fresh:
                raise RuntimeError(f"INCOMPLETE: page {page} was not empty but repeated repositories already listed, so the listing cannot be trusted to be complete")
            names += fresh
        raise RuntimeError(f"INCOMPLETE: still returning repositories after {MAX_OWNER_PAGES} pages")

    @staticmethod
    def _confirmed_not_org(e: BaseException) -> bool:
        """Only a clear "no such organisation" answer lets a bare name be tried as a user. Auth, permission, rate-limit, network and server errors do not."""
        t = err_text(e, 400).lower()
        if any(k in t for k in ("401", "403", "forbidden", "unauthor", "permission", "scope", "saml", "rate limit", "timeout", "timed out", "connection", "429", "500", "502", "503", "504", "incomplete")):
            return False
        return "404" in t or "not found" in t

    async def _expand_repos(self, out: dict, log: ToolLog) -> bool:
        """owner/repo entries stay as they are; a bare name is listed as an organisation, and only if that is confirmed not to be one, as a user. The expanded list replaces self.repos."""
        expanded: list[str] = []
        have: set[str] = set()
        ok = True
        for entry in self.repos:
            if "/" in entry:
                if entry.lower() not in have:
                    have.add(entry.lower())
                    expanded.append(entry)
                continue
            found: list[str] = []
            public_only = False
            error = ""
            if "github.list_organization_repositories" not in self.has:
                error = "github.list_organization_repositories is not available in this run"
            else:
                try:
                    found = await self._owner_repos("github.list_organization_repositories", {"org": entry}, log)
                    if not found:
                        error = f"{entry!r} is an organisation the connected account can see, but it lists no repositories"
                except Exception as e:  # noqa: BLE001
                    if self._confirmed_not_org(e):
                        if "github.list_repositories_for_a_user" not in self.has:
                            error = f"{entry!r} is not an organisation and github.list_repositories_for_a_user is not available in this run"
                        else:
                            try:
                                found, public_only = await self._owner_repos("github.list_repositories_for_a_user", {"username": entry}, log), True
                                if not found:
                                    error = f"{entry!r} is a user with no public repositories"
                            except Exception as e2:  # noqa: BLE001
                                error = f"{entry!r} is not an organisation, and listing it as a user failed: {err_text(e2, 160)}"
                    else:
                        error = f"listing {entry!r} as an organisation failed (not treated as 'not an organisation'): {err_text(e, 200)}"
            if not found:
                out["failed"].append({"source": f"github-owner:{entry}", "error": error + ". No repository of it was read. Use owner/repo to name a repository."})
                ok = False
                continue
            added = [r for r in found if r.lower() not in have]
            have |= {r.lower() for r in added}
            expanded += added
            out.setdefault("info", []).append(
                f"{entry!r} was expanded to {len(found)} repositories ({len(added)} not already listed)"
                + (": this is the user's PUBLIC repositories only (the user listing tool lists public repositories), so private repositories are not covered - name them as owner/repo" if public_only
                   else ", as far as the connected account can list them (completeness for private repositories is not claimed)"))
        self.repos = expanded
        return ok

    async def _paged(self, tool: str, args: dict, keys: tuple[str, ...], cap: int, since: str | None = None) -> list[dict]:
        """All pages of a GitHub list tool (default page size is 30, so one call silently dropped everything past it). Stops on a short page,
        at `cap` rows, at MAX_LIST_PAGES, or once the rows are older than `since`."""
        rows: list[dict] = []
        for page in range(1, MAX_LIST_PAGES + 1):
            got = _rows(await self.call(tool, {**args, "per_page": PAGE, "page": page}), *keys)
            rows += got
            if len(got) < PAGE or len(rows) >= cap:
                break
            if since and all(str(g.get("created_at") or "9") < since for g in got):
                break
        return rows[:cap]

    async def _github(self, out: dict, since: str, log: ToolLog) -> None:
        for repo in self.repos:
            try:
                await self._github_repo(out, repo, since, log)
            except Exception as e:  # noqa: BLE001
                out["failed"].append({"source": f"github:{repo}", "error": err_text(e, 200)})
        if self.repos and len(out["github"]["prs"]) >= MAX_PRS_PER_REPO * len(self.repos):
            out["capped"].append("github PRs hit the per-repo cap")

    async def _github_repo(self, out: dict, repo: str, since: str, log: ToolLog) -> None:
        owner, name = repo.split("/", 1)
        sem = asyncio.Semaphore(PARALLEL)  # bounded fan-out: same calls as before, just not one after another

        async def one(tool: str, args: dict) -> Any:
            async with sem:
                return await self.call(tool, args)

        log.add("github.search_issues", repo=repo)
        q = f"repo:{repo} is:pr author:{self.username} created:>={since[:10]}"
        prs = await self._paged("github.search_issues", {"q": q}, ("items",), MAX_PRS_PER_REPO)
        log.add("github.search_issues", repo=repo, reviewed_by=self.username)
        rv_q = f"repo:{repo} is:pr reviewed-by:{self.username} -author:{self.username} created:>={since[:10]}"
        others = await self._paged("github.search_issues", {"q": rv_q}, ("items",), 300)
        # Search results often lack merged_at. The PR list carries it, so one call per repo fills it in by number.
        try:
            log.add("github.list_pull_requests", repo=repo, state="closed")
            listed = await self._paged("github.list_pull_requests", {"owner": owner, "repo": name, "state": "closed", "sort": "created", "direction": "desc"}, ("pull_requests",),
                                       MAX_PRS_PER_REPO * 2, since)
            by_num = {x.get("number"): x for x in listed}
            for p in prs:
                full = by_num.get(p.get("number"))
                if full:
                    for k in ("merged_at", "closed_at", "merge_commit_sha", "merged", "head", "additions", "deletions", "labels"):
                        if full.get(k) not in (None, "", []) and not p.get(k):
                            p[k] = full[k]
        except Exception as e:  # noqa: BLE001 - merge state is then inferred from commits
            out["failed"].append({"source": f"github-pr-list:{repo}", "error": err_text(e, 200)})

        async def files(p: dict, allowed: bool) -> None:
            if not allowed:
                return
            try:
                p["files"] = _rows(await one("github.list_pr_files", {"owner": owner, "repo": name, "pull_number": p.get("number"), "per_page": 100}))
            except Exception:  # noqa: BLE001 - totals are used instead, flagged via size basis
                p["files"] = []

        async def reviews(p: dict) -> None:
            key = f"{repo}#{p.get('number')}"
            try:
                out["github"]["reviews"][key] = _rows(await one("github.list_pr_reviews", {"owner": owner, "repo": name, "pull_number": p.get("number"), "per_page": 100}))
            except Exception as e:  # noqa: BLE001
                out["failed"].append({"source": f"github-reviews:{key}", "error": err_text(e, 200)})

        async def pr_commits(p: dict) -> None:
            """The PR's own commit SHAs: the one hard proof that a loose commit belongs to it."""
            if "github.list_commits_on_a_pull_request" not in self.has:
                return
            try:
                rows = await self._paged("github.list_commits_on_a_pull_request", {"owner": owner, "repo": name, "pull_number": p.get("number")}, ("commits",), 250)
                p["_commit_shas"] = [str(c.get("sha")) for c in rows if c.get("sha")]
            except Exception as e:  # noqa: BLE001 - keys/files still give evidence; noted for the failed-sources list
                out["failed"].append({"source": f"github-pr-commits:{repo}#{p.get('number')}", "error": err_text(e, 200)})

        jobs = []
        for n, p in enumerate(prs):
            p["_repo"] = repo
            jobs += [files(p, n < MAX_PR_FILE_LOOKUPS), reviews(p), pr_commits(p)]
            out["github"]["prs"].append(p)
        if len(prs) > MAX_PR_FILE_LOOKUPS:
            out["capped"].append(f"pr files for {repo}: first {MAX_PR_FILE_LOOKUPS} PRs only")
        for p in others:
            p["_repo"] = repo
            jobs.append(reviews(p))
            out["github"]["prs"].append(p)
        await asyncio.gather(*jobs)
        await self._commits(out, repo, since, log)

    async def _commits(self, out: dict, repo: str, since: str, log: ToolLog) -> None:
        """Commits by the user, so repos without PRs still show work. Bots, merges and PR-covered commits are dropped in ingest."""
        owner, name = repo.split("/", 1)
        try:
            log.add("github.list_commits", repo=repo)
            rows = await self._paged("github.list_commits", {"owner": owner, "repo": name, "author": self.username, "since": since}, ("commits",), MAX_COMMITS_PER_REPO)
            if self.scope_settings and "github.list_branches" in self.has:
                from repo_scope import pages
                branches = await pages(self.call, "github.list_branches", {"owner": owner, "repo": name}, ("branches",))
                by_sha = {str(c.get("sha")): c for c in rows if c.get("sha")}
                for branch in branches:
                    ref = str(branch.get("name") or "")
                    if not ref:
                        raise RuntimeError("branch name could not be verified")
                    more = await self._paged("github.list_commits", {"owner": owner, "repo": name, "author": self.username, "since": since, "sha": ref}, ("commits",), MAX_COMMITS_PER_REPO)
                    by_sha.update({str(c.get("sha")): c for c in more if c.get("sha")})
                rows = list(by_sha.values())
        except Exception as e:  # noqa: BLE001 - named in the result
            out["failed"].append({"source": f"github-commits:{repo}", "error": err_text(e, 200)})
            return
        if len(rows) >= MAX_COMMITS_PER_REPO:
            out["capped"].append(f"commits for {repo}: first {MAX_COMMITS_PER_REPO} only")
        sem = asyncio.Semaphore(PARALLEL)
        picked = rows[:MAX_COMMITS_PER_REPO]

        pr_shas = {str(p.get("merge_commit_sha")) for p in out["github"]["prs"] if p.get("merge_commit_sha")}
        pr_shas |= {sha for p in out["github"]["prs"] for sha in (p.get("_commit_shas") or [])}
        pr_nums = {p.get("number") for p in out["github"]["prs"] if p.get("_repo") == repo}

        def covered(c: dict) -> bool:
            """Already part of one of the user's PRs on hard evidence (merge sha, or a squash message naming the PR number):
            the PR's own file list gives the size, so no get_commit call. A same title is not evidence - the commit gets its
            lookup and ingest decides on keys/files."""
            msg = str(((c.get("commit") or {}).get("message")) or c.get("message") or "").splitlines()[:1]
            first = msg[0].strip() if msg else ""
            if first.startswith(("Merge pull request #", "Merge branch ", "Merge remote-tracking")):
                return True  # a merge commit carries no work of its own (the PR does); list_pull_requests may not return merge shas
            m = re.search(r"\(#(\d+)\)\s*$", first)
            return str(c.get("sha")) in pr_shas or bool(m and int(m.group(1)) in pr_nums)

        async def detail(n: int, c: dict) -> None:
            c["_repo"] = repo
            if n < MAX_COMMIT_LOOKUPS and not c.get("files") and not covered(c):  # list_commits has no file list; size needs one lookup per commit
                try:
                    async with sem:
                        full = await self.call("github.get_commit", {"owner": owner, "repo": name, "ref": c.get("sha", "")})
                    if isinstance(full, dict):
                        c["files"], c["stats"] = full.get("files"), full.get("stats")
                except Exception:  # noqa: BLE001 - size stays unknown, the commit is still listed
                    pass

        await asyncio.gather(*(detail(n, c) for n, c in enumerate(picked)))
        if len(picked) > MAX_COMMIT_LOOKUPS:
            out["capped"].append(f"commit sizes for {repo}: first {MAX_COMMIT_LOOKUPS} commits only")
        out["github"]["commits"].extend(picked)

    async def _list_channels(self, log: ToolLog) -> list[dict]:
        """All channels the connected account can see, page by page. slack.list_channels defaults to limit=1 and public channels only,
        so ask for public and private (and DMs only when the user opted in)."""
        import adapters
        types = "public_channel,private_channel" + (",im,mpim" if self.include_dms else "")
        chans: list[dict] = []
        cursor = ""
        for _ in range(30):
            args: dict[str, Any] = {"types": types, "limit": 1000, "exclude_archived": True}
            if cursor:
                args["cursor"] = cursor
            resp = await self.call("slack.list_channels", args)
            if bad := adapters.failed_payload(resp):
                raise RuntimeError(bad)
            chans += _rows(resp, "channels")
            cursor = str(adapters.find_key(resp, "next_cursor") or "")
            if not cursor:
                break
        return chans

    async def _slack(self, out: dict, since: str, log: ToolLog, picked: list[dict]) -> None:
        from guards import is_dm_channel
        try:
            if self.include_dms and not any(is_dm_channel(c) for c in picked):
                out["capped"].append("include_dms=Yes but no DM was listed: add a person's Slack user ID (or *dm* for all) to the channels box to read it")
            if "slack.search_messages" in self.has and "slack.fetch_message_thread_from_a_conversation" in self.has:
                import adapters
                await adapters.fetch_slack_search(self.call, [c for c in picked if not is_dm_channel(c)], since, log, out)
                picked = [c for c in picked if is_dm_channel(c)]  # opted-in DMs still read from history, channel by channel
            for ch in picked:
                log.add("slack.read_history", channel=ch["id"], is_dm=is_dm_channel(ch))
                msgs = _rows(await self.call("slack.read_history", {"channel": ch["id"], "oldest": since, "limit": 1000}), "messages")
                groups: dict[str, list[dict]] = {}
                for m in msgs:
                    groups.setdefault(str(m.get("thread_ts") or m.get("ts")), []).append(m)
                for ts, ms in groups.items():
                    out["slack"]["threads"].append({"channel": ch.get("name", ""), "channel_id": ch["id"], "messages": ms})
        except Exception as e:  # noqa: BLE001
            out["failed"].append({"source": "slack", "error": err_text(e, 200)})
