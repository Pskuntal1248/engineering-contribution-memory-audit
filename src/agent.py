"""UC-10 Engineering Contribution Memory. Read-only. Plain LangGraph, async graph().

Pipeline: load memory -> fetch sources -> normalize/dedupe/filter -> link + analyse -> render -> save memory.
Code does the facts and enforces the rules; the model (if used) only labels and words, and its output is
validated against real item ids."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

import source_setup
import copy
import eprofile
import outputs
from guards import MODE_LIMITS, ToolLog, budget_for, internal_terms, parse_channels
from models import Item, day, local_date_of, local_tz
from pipeline import Config, ingest
from sources import GatewaySource, Source
from store import NativeMemoryStore, PlatformStore, ProfileStore, platform_store
from ask import parse_team_changes
from inputs import backfill_since, parse_months, validate_inputs, repo_entries
from weekly import new_items, next_cursors, quiet_week_line, since_for, id_key

ON_DEMAND = {"weekly_summary", "one_on_one", "review_material", "interview_stories", "evidence_gaps"}


class State(TypedDict, total=False):
    inputs: dict
    memory: dict
    ctx: dict
    corpus: dict
    result: dict


def _payload(raw: Any) -> Any:
    """A tool result as data. Gateway refusals arrive as text, not JSON: raise, never pass them on."""
    if isinstance(raw, list):
        raw = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(raw[:300]) from None
    if isinstance(raw, dict) and raw.get("successful") is False:
        raise RuntimeError(str(raw.get("error"))[:300])
    return raw.get("data", raw) if isinstance(raw, dict) else raw


class Deps:
    """Everything the graph needs from outside. Tests pass fixtures and a fake model; graph() passes the gateway."""

    def __init__(self, source_factory, llm=None, store_factory=None, now=None, embedder=None, identity=None, site=None):
        self.source_factory, self.llm, self.store_factory, self.embedder = source_factory, llm, store_factory, embedder
        self.site = site  # async () -> Jira base URL from the connected Jira; None when unavailable
        self.identity = identity  # async () -> {"login": ..., "name": ...} read from the connected GitHub account; None when unavailable
        self.now = now or (lambda: datetime.now(timezone.utc))


def _docs_sources(raw: str) -> list[str]:
    if raw.strip().lower() == "all three":
        return ["confluence", "notion", "google drive"]
    return [d.lower() for d in (x.strip() for x in raw.replace("+", ",").split(",")) if d and d.lower() != "none"]


def _cfg(inp: dict) -> Config:
    user = str(inp.get("github_username", "")).strip()
    return Config(
        username=user, repos=[r.strip() for r in str(inp.get("repos_or_org", "")).replace("\n", ",").split(",") if r.strip()],
        jira_site=str(inp.get("jira_site", "")), jira_projects=[p.strip().upper() for p in str(inp.get("jira_projects", "")).replace("\n", ",").split(",") if p.strip()],
        docs_sources=_docs_sources(str(inp.get("docs_source", ""))),
        exclude_repos=[r.strip() for r in str(inp.get("exclude_repos", "")).replace("\n", ",").split(",") if r.strip()],
        slack_channels=parse_channels(str(inp.get("slack_channels", ""))), include_dms=str(inp.get("include_dms", "No")).lower() in {"yes", "true"},
        slack_user_ids=set(parse_channels(str(inp.get("slack_user_ids", "")))), name_hints=[user, *parse_channels(str(inp.get("display_name", "")))],
    )


def make_graph(deps: Deps):
    def local_now(inp):
        """Now in the user's timezone input. Gravity fires the weekly schedule on India time only, so every window and label is computed here."""
        t = deps.now()
        return t.astimezone(local_tz(inp.get("timezone"))) if t.tzinfo else t

    async def load(state: State) -> dict:
        inp = dict(state.get("inputs", {}))
        mem = state.get("memory")
        if deps.store_factory:
            store = deps.store_factory(mem)
        else:
            ps = platform_store()
            store = PlatformStore(ps, mem) if ps is not None else NativeMemoryStore(mem, 256)
        prior = await store.aload() if hasattr(store, "aload") else store.load()
        if prior.get('_corrupt') or prior.get('_store_empty'):
            problems = validate_inputs(inp)
            detail = "\n- " + "\n- ".join(problems) if problems else ""
            raise ValueError('Saved history is unreadable or missing from storage. No source reads or state changes were made. Restore the saved history before continuing; this run will not silently reset or backfill it.' + detail)
        inp, setup = source_setup.resolve(prior, inp)
        problems = validate_inputs(inp)
        if setup.get('pending_config'):
            problems += ['pending source setup: ' + p for p in validate_inputs({**inp, **setup['pending_config']})]
        if not problems and str(inp.get("backfill_months") or "").strip():
            try:
                backfill_since(deps.now(), parse_months(inp.get("backfill_months")) or 6)
            except ValueError as e:
                problems.append(str(e))
        if problems:  # explicit, all at once, before any source call or model call
            raise ValueError("The run was stopped before reading anything. Fix these inputs and run again:\n- " + "\n- ".join(problems))
        if str(inp.get("request") or "").strip():
            inp["request"] = str(inp["request"]).strip().lower()
        mem = state.get("memory")
        id_notes: list[str] = []
        if not str(inp.get("timezone") or "").strip():
            id_notes.append("timezone was not given, so the default Asia/Kolkata was used for dates and periods")
        if not str(inp.get("github_username") or "").strip():  # the user's typed value always wins; only a blank is filled from the connected account
            who: dict = {}
            if deps.identity:
                try:
                    who = await deps.identity() or {}
                except Exception:  # noqa: BLE001
                    who = {}
            login = str(who.get("login") or "").strip()
            if login:
                inp["github_username"] = login
                id_notes.append(f"GitHub username taken from the connected GitHub account: {login} (GitHub login for attribution, separate from your display name). A display name entered in the form takes priority over the account name. If that login is not you, type your GitHub username and run again")
                if not str(inp.get("display_name") or "").strip() and str(who.get("name") or "").strip():
                    inp["display_name"] = str(who["name"]).strip()
            else:
                raise RuntimeError("Could not read your GitHub username from the connected GitHub account. Connect GitHub, or type your username in 'Your GitHub username' and run again.")
        # Jira is optional. Only resolve its site when the user selected projects.
        if deps.site and str(inp.get("request") or "") not in ON_DEMAND and inp.get("jira_projects") and not str(inp.get("jira_site") or "").strip():
            try:
                base = str(await deps.site() or "").strip()
            except Exception as e:
                from coverage import unavailable
                if not unavailable(str(e)):
                    raise RuntimeError("Could not verify the selected Jira connection; retry or check it") from e
                base = ""
            if base.startswith("https://"):
                inp["jira_site"] = base.rstrip("/")
                id_notes.append(f"Jira site taken from the connected Jira account: {inp['jira_site']}")
            else:
                inp["_jira_unavailable"] = True
        req = str(inp.get("request") or "auto")
        if req == "weekly_update":
            req = "auto"  # the dropdown default: the first run backfills, every later run is the weekly update (new items only)
        has_memory = bool(prior.get("items") or prior.get("last_run"))  # an empty but completed run still counts
        if req == "auto":
            req = "weekly" if has_memory or prior.get("source_setup") else "backfill"  # weekly only after a backfill really completed
        if req in ON_DEMAND and not has_memory:
            raise ValueError("No saved evidence for this output. Run the explicit initial backfill/enrichment first; this output will not fetch sources.")
        selected = sorted({r.lower() for r in repo_entries(inp.get("repos_or_org"))} - {r.lower() for r in repo_entries(inp.get("exclude_repos"))})
        if not selected:
            raise ValueError("All selected repositories are excluded. No source reads or state changes.")
        scope_changed = selected != prior.get("selected_repos")
        if inp.get("jira_site") and not setup["config"].get("jira_site"):
            setup["config"]["jira_site"] = inp["jira_site"]
        active_scopes = source_setup.scopes(setup["config"])
        sync = copy.deepcopy(prior.get("scope_sync") or {})
        # Migration: only exact scopes explicitly recorded complete can be reused. Broad saved evidence is retained,
        # but an old shared timestamp cannot prove any per-scope successful sync.
        pending = [key for key in active_scopes if not sync.get(key, {}).get("complete")]
        if req == "enrich_new_scopes":
            req = "enrichment"
        setup["config"]["github_username"] = inp["github_username"]
        return {"inputs": inp, "ctx": {"started": time.monotonic(), "mode": req, "source_setup": setup, "active_scopes": active_scopes, "pending_scopes": pending, "scope_sync": sync, "selected_repos": selected, "prior": prior, "store": store, "notes": id_notes + (["memory was unreadable, starting fresh"] if prior.get("_corrupt") else (["the saved history was missing from the store, so this run backfills again"] if prior.get("_store_empty") else []))}}

    async def fetch(state: State) -> dict:
        ctx = state["ctx"]
        inp = state["inputs"]
        cfg = _cfg(inp)
        if inp.get("_jira_unavailable"):
            cfg.jira_projects = []
        ctx["cfg"], ctx["log"] = cfg, ToolLog()
        mode = ctx["mode"]
        if mode in ON_DEMAND or mode == "update_source_setup":  # saved-only outputs/setup edits never read contribution sources
            ctx["raw"] = {"failed": [], "capped": []}
            return {"ctx": ctx}
        start = backfill_since(deps.now(), parse_months(inp.get("backfill_months")) or 6)
        until = deps.now().isoformat().replace("+00:00", "Z")
        ctx["since"] = start
        combined = {"github": {"prs": [], "reviews": {}, "commits": []}, "jira": [], "slack": {"channels": [], "threads": []}, "docs": [], "failed": [], "capped": [], "coverage": {}}
        scopes = ctx["active_scopes"]
        targets = [k for k in scopes if not ctx["scope_sync"].get(k, {}).get("complete") or ctx["scope_sync"].get(k, {}).get("status") == "incomplete"] if mode in {"backfill", "enrichment"} else [k for k in scopes if ctx["scope_sync"].get(k, {}).get("complete")]
        for key in targets:
            spec = scopes[key]
            single_cfg = source_setup.scoped_config(cfg, spec)
            src = deps.source_factory(single_cfg)
            if hasattr(src, "allow_non_github_scope"):
                src.allow_non_github_scope = True
            if hasattr(src, "deadline"):
                src.deadline = ctx.get("started", time.monotonic()) + MODE_LIMITS["backfill" if mode in {"backfill", "enrichment"} else "weekly"][1] * 0.73
            if hasattr(src, "configure_scope"):
                src.configure_scope([], start, until)
            old_sync = ctx["scope_sync"].get(key, {})
            since = since_for(old_sync.get("cursor"), start) if mode == "weekly" or old_sync.get("complete") else start
            try:
                raw = {"failed": [{"source": key, "error": "Jira unavailable"}], "coverage": {"jira": "isn't connected"}} if inp.get("_jira_unavailable") and spec["source"] == "jira" else await src.fetch(since, until, ctx["log"])
            except Exception as e:
                raw = {"failed": [{"source": key, "error": str(e)[:200]}]}
            success = source_setup.scope_success(raw, spec)
            ctx["scope_sync"][key] = {**old_sync, "complete": bool(old_sync.get("complete")) or success, "cursor": until if success else old_sync.get("cursor", ""),
                                      "last_attempt": until, "window_start": old_sync.get("window_start", start), "status": "complete" if success else "incomplete"}
            gh = raw.get("github", {})
            combined["github"]["prs"].extend(gh.get("prs", []))
            combined["github"]["commits"].extend(gh.get("commits", []))
            combined["github"]["reviews"].update(gh.get("reviews", {}))
            combined["jira"].extend(raw.get("jira", []))
            for field in ("channels", "threads"):
                combined["slack"][field].extend(raw.get("slack", {}).get(field, []))
            combined["slack"]["my_user_ids"] = sorted(set(combined["slack"].get("my_user_ids", [])) | set(raw.get("slack", {}).get("my_user_ids", [])))
            if raw.get("slack", {}).get("permalink_base"):
                combined["slack"]["permalink_base"] = raw["slack"]["permalink_base"]
            for field in ("docs", "failed", "capped"):
                combined[field].extend(raw.get(field, []))
            combined["coverage"].update({k: v for k, v in raw.get("coverage", {}).items() if k == spec["source"]})
            if not success:
                combined["failed"].append({"source": key, "error": "Scope incomplete/unavailable. Successful cursor was not advanced; explicit enrichment retries this scope."})
        for name, present in (("jira", cfg.jira_projects), ("slack", cfg.slack_channels), ("docs", cfg.docs_sources)):
            combined["coverage"].setdefault(name, "covered" if present else {"jira": "no projects selected", "slack": "no channels selected", "docs": "no docs source selected"}[name])
        if inp.get("_jira_unavailable"):
            combined["coverage"]["jira"] = "isn't connected"
        ctx["pending_scopes"] = [k for k in scopes if not ctx["scope_sync"].get(k, {}).get("complete")]
        ctx["raw"] = combined
        ctx["raw"]["repo_scope"] = {"mode": "allow-list", "read": [scopes[k]["repo"] for k in targets if scopes[k]["source"] == "github"], "excluded": cfg.exclude_repos, "unverified": [scopes[k]["repo"] for k in ctx["pending_scopes"] if scopes[k]["source"] == "github"], "no_commits": []}
        return {"ctx": ctx}

    async def analyse(state: State) -> dict:
        ctx, inp, cfg = state["ctx"], state["inputs"], state["ctx"]["cfg"]
        mode, prior = ctx["mode"], ctx["prior"]
        names = {n for n in cfg.name_hints if n}
        budget = budget_for("backfill" if mode in {"backfill", "enrichment"} else "weekly" if mode == "weekly" else "on_demand")
        budget.started = ctx.get("started", budget.started)  # the spec's time limit covers the whole run, source reads included
        old = [eprofile.expand(r) for r in prior.get("items", [])]
        new_ids: set[str] = set()
        notes = list(ctx["notes"])
        failed = list(ctx["raw"].get("failed", []))
        notes += [f"capped: {c}" for c in dict.fromkeys(ctx["raw"].get("capped", []))]
        notes += list(ctx["raw"].get("info", []))
        if ctx["raw"].get("blocked"):  # preflight stopped the read: no analysis and no model calls on unverified inputs
            ctx["items"], ctx["new_ids"], ctx["failed"], ctx["notes"], ctx["budget"], ctx["prof"] = [], set(), failed, notes, budget, None
            return {"ctx": ctx}
        if mode in {"backfill", "weekly", "enrichment"}:
            ing = ingest(ctx["raw"], cfg, ctx["log"])
            seen = set(prior.get("seen", []))
            fresh, already = new_items(ing.items, seen)
            new_ids = {i.id for i in fresh}
            have = {i.id for i in old}
            # Updates to a known item replace its saved detail, rather than disappear behind seen IDs.
            incoming = {i.id: i for i in ing.items}
            items = [incoming.pop(i.id, i) for i in old] + list(incoming.values())
            unread = [i for i in ing.items if i.type == "doc" and not i.meta.get("body_read", True)]
            if unread:  # confidentiality can only be judged on what was read
                shown = ", ".join(repr(i.title[:40]) for i in unread[:3]) + (" ..." if len(unread) > 3 else "")
                notes.append(f"confidential markers were checked in the title and labels only for {len(unread)} doc(s) whose body was not read ({shown}); a marker inside those bodies cannot be seen")
            if ing.skipped:
                why: dict[str, int] = {}
                for sk in ing.skipped:
                    why[sk.reason] = why.get(sk.reason, 0) + 1
                top = sorted(why.items(), key=lambda kv: -kv[1])
                notes.append(f"Rows not counted ({len(ing.skipped)}): " + "; ".join(f"{r} ({n})" for r, n in top[:12]) + ".")
            if ing.not_found_channels:
                known = {f.get("source") for f in failed}
                failed += [{"source": f"slack:#{c}", "error": "channel not found"} for c in ing.not_found_channels if f"slack:#{c}" not in known]
        else:
            items = old
        ctx["all_items"] = items
        selected = set(ctx["selected_repos"])
        outside = [i for i in items if not source_setup.in_view(i, ctx["source_setup"]["config"])]
        items = [i for i in items if source_setup.in_view(i, ctx["source_setup"]["config"])]
        new_ids &= {i.id for i in items}
        if outside:
            notes.append(f"{len(outside)} previously saved items are outside the active source setup (or have no verifiable scope identity). Retained in saved history, excluded from this analysis and output.")
        tz = local_tz(inp.get("timezone"))
        for it_ in items:  # calendar days are shown in the user's timezone; the stored time stays UTC (ordering, cursors)
            it_.meta["local_date"] = local_date_of(it_.date, tz)
        ctx["items"], ctx["new_ids"], ctx["failed"] = items, new_ids, failed
        if mode == "update_source_setup":
            ctx["prof"], ctx["notes"], ctx["budget"] = None, notes, budget
            return {"ctx": ctx}
        if ctx["source_setup"].get("pending_config"):
            notes.append("New source selections saved as pending setup. Choose enrich_new_scopes to activate and backfill them separately; the current output uses existing evidence only.")
        if ctx["pending_scopes"]:
            notes.append("Not covered: selected scopes pending enrichment: " + ", ".join(ctx["pending_scopes"]) + ". Choose enrich_new_scopes separately, then request the output again. No hidden backfill inside this output.")
        prof = await eprofile.build(items, username=cfg.username, names=names, llm=deps.llm, budget=budget,
                                    framework=str(inp.get("review_framework", "") or ""), role_and_level=str(inp.get("role_and_level", "")),
                                    prior=prior, repo_aliases=cfg.repo_aliases, embedder=deps.embedder,
                                    team_changes=parse_team_changes(str(inp.get("team_changes", "") or "")), reuse_names=mode != "backfill",
                                    new_ids=None if mode == "backfill" else new_ids)
        notes += prof["notes"]
        ctx["prof"], ctx["notes"], ctx["budget"] = prof, notes, budget
        return {"ctx": ctx}

    async def render(state: State) -> dict:
        ctx, inp, cfg = state["ctx"], state["inputs"], state["ctx"]["cfg"]
        if ctx["mode"] == "update_source_setup":
            corpus = {"title": "Source setup saved", "summary": f"Revision {ctx['source_setup']['revision']}. No source or model calls. Existing evidence retained; removed scopes hidden from active views.",
                      "sections": [{"heading": "Next step", "body": "Choose enrich_new_scopes to backfill pending scopes separately. Then request your output.", "bullets": ctx["pending_scopes"]}]}
            result = {"status": "setup_saved", "setup_revision": ctx["source_setup"]["revision"], "pending_scopes": ctx["pending_scopes"], "model_calls": 0}
            ctx["corpus"], ctx["result"] = corpus, result
            return {"ctx": ctx, "corpus": corpus, "result": result}
        if ctx["raw"].get("blocked"):  # a stopped run says so - it must not look like a completed profile
            bullets = [f"{f.get('source')}: {f.get('error')}" for f in ctx["failed"]]
            corpus = {"title": "Run stopped - fix the inputs and rerun",
                      "summary": "Nothing was read, no model calls were made, and the saved profile was left unchanged, because configured sources could not be verified.",
                      "sections": [{"heading": "Fix these, then rerun", "bullets": bullets}]}
            result = {"status": "blocked", "model_calls": 0, "seconds": round(ctx["budget"].elapsed(), 1), "new_items": 0,
                      "dm_reads": len(ctx["log"].dm_reads()),
                      "page_summary": corpus["summary"], "page_sections": [sec["heading"] for sec in corpus["sections"]],
                      "page_chars": len(outputs.to_markdown(corpus, with_tables=False))}
            outputs.write_local_copy(corpus, result=result)
            ctx["corpus"], ctx["result"] = corpus, result
            return {"ctx": ctx, "corpus": corpus, "result": result}
        items, prof, mode = ctx["items"], ctx["prof"], ctx["mode"]
        names = {n for n in cfg.name_hints if n}
        colleagues = sorted({a for i in items for a in [i.author, *i.co_authors, *(i.meta.get("participants") or [])] if a and a not in names and a.lower() != cfg.username.lower()})
        terms = internal_terms(items, [t for t in str(inp.get("redact_terms", "")).replace("\n", ",").split(",")], cfg.repos[0].split("/")[0] if cfg.repos else "", cfg.jira_site)
        window = ctx["new_ids"]
        if mode == "weekly_summary":  # on request: the last 7 days of the saved profile
            cut = (local_now(inp) - timedelta(days=7)).strftime("%Y-%m-%d")
            window = {i.id for i in items if day(i) >= cut}
        since = ctx.get("since", "")[:10] or (local_now(inp) - timedelta(days=7)).strftime("%Y-%m-%d")
        window_label = f"dated since {cut} (the last 7 days)" if mode == "weekly_summary" else "new since the last run"
        quiet = quiet_week_line(since, local_now(inp).strftime("%Y-%m-%d")) if mode in {"weekly", "weekly_summary"} and not window else None
        title = {"backfill": "Engineering profile", "weekly": "Weekly update", "weekly_summary": "Weekly summary", "one_on_one": "1:1 talking points",
                 "review_material": "Review material", "interview_stories": "Interview stories", "evidence_gaps": "Evidence gaps", "enrichment": "New-scope enrichment"}[mode]
        source_coverage = ctx["raw"].get("coverage") or ctx["prior"].get("coverage", {})
        corpus, result = outputs.corpus_for("profile" if mode == "enrichment" else mode, prof, items, title=f"{title} - {cfg.username}" if cfg.username else title, terms=terms, keep_internal=str(inp.get("keep_internal_details", "No")).lower() == "yes",
                                            colleagues=colleagues, names=names, date_from=str(inp.get("period_from", "") or ""), date_to=str(inp.get("period_to", "") or ""),
                                            run_notes=ctx["notes"], failed=ctx["failed"], quiet_line=quiet, new_ids=window, window_label=window_label)
        from coverage import line as coverage_line
        source_coverage = ctx["raw"].get("coverage") or ctx["prior"].get("coverage", {})
        from repo_scope import summary as scope_summary
        scope = ctx["raw"].get("repo_scope") or {"mode": "selected-saved-view", "read": ctx["selected_repos"], "excluded": repo_entries(inp.get("exclude_repos")), "unverified": [], "no_commits": []}
        if scope:
            scope_text = scope_summary(scope)
            if mode == "interview_stories" and str(inp.get("keep_internal_details", "No")).lower() != "yes":
                from guards import scrub
                scope_text = scrub(scope_text, terms, False, False)
            corpus["summary"] = scope_text + "\n\n" + corpus.get("summary", "")
            result["repo_scope"] = scope
        notice = coverage_line(source_coverage)
        if notice:
            corpus["summary"] = corpus.get("summary", "") + "\n\n" + notice
        result["source_coverage"] = source_coverage
        result["setup_revision"] = ctx["source_setup"]["revision"]
        result["pending_scopes"] = ctx["pending_scopes"]
        result["scope_sync"] = ctx["scope_sync"]
        result["pending_source_setup"] = ctx["source_setup"].get("pending_config")
        if mode == "enrichment":
            result["mode"] = "enrichment"
            corpus["summary"] = "Separate backfill-budget enrichment of new/incomplete scopes. Request your 1:1/interview/review output after this completes. " + corpus["summary"]
        result["model_diagnostics"] = list(getattr(deps.llm, "diagnostics", []))
        result["status"] = "completed"
        result["model_calls"] = ctx["budget"].calls  # every attempt on the wire: chat (retries included) plus embeddings (retries and failures included)
        result["embedding_calls"] = ctx["budget"].by_kind.get("embedding", 0)
        result["chat_calls"] = ctx["budget"].calls - result["embedding_calls"]
        result["seconds"] = round(ctx["budget"].elapsed(), 1)
        result["new_items"] = len(ctx["new_ids"])
        wanted_docs = _docs_sources(str(inp.get("docs_source", "")))
        result["docs_sources_asked"] = wanted_docs
        def _doc_src(i: Any) -> str:
            parts = i.id.split(":")
            return {"drive": "google drive"}.get(parts[1].lower(), parts[1].lower()) if len(parts) >= 3 else ""
        result["docs_items_by_source"] = {s_: sum(1 for i in items if i.type == "doc" and _doc_src(i) == s_) for s_ in wanted_docs}
        result["dm_reads"] = len(ctx["log"].dm_reads())
        # The report itself is the corpus (the page Gravity shows in full) plus the ledger table (CSV download). The result box only
        # gets a short summary: the headline, the section list and the ledger size. No long text there, so nothing looks cut off.
        result["page_summary"] = corpus.get("summary", "")
        result["page_sections"] = [str(sec.get("heading", "")) for sec in corpus.get("sections", [])]
        result["page_chars"] = len(outputs.to_markdown(corpus, with_tables=False))
        outputs.write_local_copy(corpus, result=result)
        ctx["corpus"], ctx["result"] = corpus, result
        return {"ctx": ctx, "corpus": corpus, "result": result}

    async def persist(state: State) -> dict:
        ctx, inp = state["ctx"], state["inputs"]
        if ctx["mode"] == "update_source_setup":
            profile = {**ctx["prior"], "source_setup": ctx["source_setup"], "scope_sync": ctx["scope_sync"]}
            store = ctx["store"]
            memory = await store.adump(profile) if hasattr(store, "adump") else store.dump(profile)
            return {"memory": memory}
        if ctx["mode"] in ON_DEMAND and ctx.get("prof") and hasattr(ctx["store"], "asave_framework"):  # only the cache entries; the profile itself is not touched
            if ctx["prof"].get("framework_cache_new"):
                await ctx["store"].asave_framework(ctx["prof"]["framework_cache"])
            if ctx["prof"].get("mapping_cache_new"):
                await ctx["store"].asave_mapping(ctx["prof"]["mapping_cache"])
        if ctx["mode"] in ON_DEMAND and ctx["source_setup"] != ctx["prior"].get("source_setup"):
            profile = {**ctx["prior"], "source_setup": ctx["source_setup"], "scope_sync": ctx["scope_sync"]}
            store = ctx["store"]
            memory = await store.adump(profile) if hasattr(store, "adump") else store.dump(profile)
            return {"memory": memory}
        if ctx["mode"] in ON_DEMAND or ctx["raw"].get("blocked"):
            return {}  # on-demand answers and stopped runs never change the saved profile
        prof, items, prior = ctx["prof"], ctx["items"], ctx["prior"]
        all_items = ctx.get("all_items", items)
        current_ids = {i.id for i in items}
        retained = {r["id"]: r for r in prior.get("items", []) if r["id"] not in current_ids}
        po = prof["project_of"]
        profile = {
            "source_setup": ctx["source_setup"],
            "scope_sync": ctx["scope_sync"],
            "grouping_version": eprofile.GROUPING_VERSION,
            "selected_repos": ctx["selected_repos"],
            "selected_scope_complete": all(ctx["scope_sync"].get(k, {}).get("complete") and ctx["scope_sync"].get(k, {}).get("status") == "complete" for k in ctx["active_scopes"] if k.startswith("github:")),
            "selected_github_cursor": next_cursors({}, [i for i in items if i.source == "github"]).get("github", ""),
            "items": [retained.get(i.id) or eprofile.compact(i, po.get(i.id, "")) for i in sorted(all_items, key=lambda x: (x.date, x.id))],
            "seen": sorted(set(prior.get("seen", [])) | {id_key(i.id) for i in all_items} | {id_key(m) for i in all_items for m in i.meta.get("merged_ids", [])}),
            "cursors": next_cursors(prior.get("cursors", {}), [i for i in all_items if i.source and not ctx["pending_scopes"]]),
            "verdicts": {**prior.get("verdicts", {}), **prof["verdicts"]}, "cards": prof["card_store"], "dverdicts": {**prior.get("dverdicts", {}), **prof.get("dverdicts", {})}, "last_run": local_now(inp).strftime("%Y-%m-%d"),
            "source_selection": {"jira": str(inp.get("jira_projects") or ""), "slack": str(inp.get("slack_channels") or ""), "docs": str(inp.get("docs_source") or "None")},
            "coverage": ctx["raw"].get("coverage", {}),
            "repo_scope": ctx["raw"].get("repo_scope", {}),
            "backfill_done": not bool(ctx["pending_scopes"]),
            "gaps": [{"id": g["id"], "status": g["status"]} for g in prof["gaps"]],
        }
        if prof.get("framework_cache") or prior.get("framework"):
            profile["framework"] = prof.get("framework_cache") or prior.get("framework")
        if prof.get("mapping_cache") or prior.get("mapping"):
            profile["mapping"] = prof.get("mapping_cache") or prior.get("mapping")
        if ctx["raw"].get("github_incomplete"):
            # Newest observed item is not proof that older pages were covered.
            profile["cursors"]["github"] = (prior.get("cursors") or {}).get("github", "")
            profile["backfill_done"] = bool(prior.get("backfill_done"))
            profile["selected_github_cursor"] = prior.get("selected_github_cursor", "") if prior.get("selected_repos") == ctx["selected_repos"] else ""
        store = ctx["store"]
        memory = await store.adump(profile) if hasattr(store, "adump") else store.dump(profile)
        # memory is written only here, after the work above succeeded
        return {"memory": memory}

    wf = StateGraph(State)
    for n, f in (("load", load), ("fetch", fetch), ("analyse", analyse), ("render", render), ("persist", persist)):
        wf.add_node(n, f)
    wf.set_entry_point("load")
    for a, b in (("load", "fetch"), ("fetch", "analyse"), ("analyse", "render"), ("render", "persist"), ("persist", END)):
        wf.add_edge(a, b)
    return wf.compile()


async def graph():
    # Environment variables are read here, inside the function, never at import time.
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from llm import GatewayLLM

    token = os.environ["GRAVITY_RUN_TOKEN"]
    client = MultiServerMCPClient({"gravity": {"transport": "streamable_http", "url": os.environ["GRAVITY_GATEWAY_URL"],
                                               "headers": {"Authorization": f"Bearer {token}"}}})
    tools = {t.name: t for t in await client.get_tools()}  # only the tools declared in manifest.yaml

    async def call(name: str, args: dict):
        tool = tools.get(name.replace(".", "_"))
        if tool is None:
            raise RuntimeError(f"{name} is not allowed in this run")
        return _payload(await tool.ainvoke(args))

    has = {k.replace("_", ".", 1) for k in tools}
    llm = GatewayLLM(os.environ["GRAVITY_LLM_URL"], token)
    from embed import GatewayEmbedder
    embedder = GatewayEmbedder(os.environ["GRAVITY_LLM_URL"], token)

    async def identity():
        if "github.get_the_authenticated_user" not in has:
            return {}
        me = await call("github.get_the_authenticated_user", {})
        from adapters import find_key
        return {"login": find_key(me, "login"), "name": find_key(me, "name")}

    async def site():
        if "jira.get_server_info" not in has:
            return ""
        from adapters import find_key
        return find_key(await call("jira.get_server_info", {}), "baseUrl", "base_url")

    def source_factory(cfg: Config):
        return GatewaySource(call, cfg.username, cfg.repos, cfg.slack_channels, cfg.include_dms, cfg.jira_projects, has, cfg.docs_sources, cfg.name_hints)

    return make_graph(Deps(source_factory, llm, embedder=embedder, identity=identity, site=site))
