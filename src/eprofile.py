"""Build and update the engineering profile from items. Deterministic core + optional model passes."""
from __future__ import annotations

from collections import Counter
from typing import Any

import analysis
from guards import TRIVIAL_TITLE
import linking
import metrics
from guards import Budget, Statement, cap_role, confidential_ids, drop_colleague_assessments, keep_cited, predicts_promotion
import embed
import ladder
from ladder import Expectation, parse_framework, statuses
from models import Item
from weekly import id_key

TEXT_KEEP = 160


import re as _re


def outputs_id(n: str) -> bool:
    return bool(_re.fullmatch(r"[UW](?=[A-Z0-9]*\d)[A-Z0-9]{8,11}", n or ""))


def compact(it: Item, project: str) -> dict[str, Any]:
    m = it.meta
    from interview import authored_text, proposed
    keep = {k: m[k] for k in ("mine", "confidential", "repo", "number", "merged_flag", "reverted", "is_revert", "received", "reopen_count", "status", "channel", "merged_ids", "state", "my_messages", "generated_only", "docs_only", "cycle_hours", "participants", "paths") if k in m}
    if it.type in {"doc", "pr"} and analysis._DECISION.search(it.text or ""):
        keep["decision_text"] = True  # the stored text is cut short; remember that the full text had decision wording
    if analysis.is_decision_candidate(it):  # the stored text is cut short, so keep the decision sentence and owner from the full text
        keep["dsum"] = analysis.decision_sentence(it.text)
        keep["owner"] = analysis.decision_owner(it.text)
    if it.type in {"doc", "pr"} and analysis.NEEDS_DECISION.search(it.text or ""):
        keep["needs_decision"] = True
    if it.type == "thread":
        keep["interview_action"] = authored_text(it)[:1200]
    if it.type in {"doc", "pr"}:
        keep["interview_proposed"] = proposed(it)
    keep["size"] = (m.get("size") or {}).get("lines") if m.get("size") else None
    return {"id": it.id, "s": it.source, "t": it.type, "date": it.date, "title": it.title[:160], "url": it.url, "text": it.text[:TEXT_KEEP],
            "author": it.author, "co": it.co_authors[:4], "keys": it.keys[:5], "p": project, "m": keep}


def expand(row: dict[str, Any]) -> Item:
    m = dict(row.get("m", {}))
    if m.get("size") is not None:
        m["size"] = {"lines": m["size"], "excluded": 0, "files": 0, "basis": "stored"}
    if row["t"] == "pr" and "generated_only" not in m:  # memory saved before the flag was stored: judge from the title
        import normalize
        m["generated_only"] = bool(normalize._LOCK_WORDS.search(row.get("title", "")))
    if row["t"] == "pr" and "docs_only" not in m:
        import normalize
        m["docs_only"] = normalize.docs_only_pr(None, row.get("title", ""))
    if row.get("p"):
        m["saved_project"] = row["p"]  # the project this item belonged to at the last backfill (the stored text is cut short, so re-linking alone can lose it)
    return Item(id=row["id"], source=row["s"], type=row["t"], date=row["date"], title=row["title"], url=row["url"], text=row["text"],
                author=row.get("author", ""), co_authors=row.get("co", []), keys=row.get("keys", []), meta=m)


MIN_PROJECT_ITEMS = 2  # a project needs at least 2 linked items


def _pending_chat_calls(items: list[Item], *, llm, budget: Budget, framework: str, role_and_level: str, prior: dict[str, Any] | None, repo_aliases: dict[str, str]) -> tuple[int, dict[str, int]]:
    """Upper bound of the chat requests this run still has to make after the embedding step: thread labels, decision wording, project cards, the framework
    extraction and the evidence mapping. Same batch sizes as the passes themselves; the project count comes from a keyword-only linking pass."""
    import inspect
    if llm is None:
        return 0, {}
    pr = prior or {}
    cthr = inspect.signature(analysis.classify_threads_with_model).parameters
    threads = [i for i in items if i.type == "thread" and i.meta.get("mine") and i.id not in pr.get("verdicts", {})]
    dec = [i for i in items if analysis.is_decision_candidate(i) and i.id not in pr.get("dverdicts", {})]
    cdec = inspect.signature(analysis.classify_decisions_with_model).parameters
    n_proj = 0
    try:
        clusters0 = linking.link(items, repo_aliases, dense=None)
        n_proj = sum(1 for c in clusters0.values() if len(c["item_ids"]) >= MIN_PROJECT_ITEMS)
    except Exception:  # noqa: BLE001 - an estimate only
        n_proj = 0
    per_card = inspect.signature(analysis.project_cards_with_model).parameters["per_call"].default
    exps = parse_framework(framework, role_and_level) if framework.strip() else []
    fw_calls, mapping_exps = 0, (exps or None)
    if framework.strip() and not exps:
        saved = pr.get("framework") or {}
        if saved.get("hash") == ladder.framework_hash(framework, role_and_level) and saved.get("exps"):
            mapping_exps = [Expectation(**e) for e in saved["exps"]]
        else:
            fw_calls = min(ladder.MAX_WINDOWS, -(-len(framework) // ladder.WINDOW))
    parts = {
        "thread labels": min(cthr["max_batches"].default, -(-len(threads) // cthr["batch"].default)),
        "decision wording": min(cdec["max_batches"].default, -(-len(dec) // cdec["batch"].default)),
        "project cards": -(-n_proj // per_card),
        "framework reading": fw_calls,
        "evidence mapping": ladder.pending_mapping_requests(mapping_exps, items, pr.get("mapping")) if framework.strip() else 0,
    }
    return sum(parts.values()) + ladder.MAP_RESERVE, parts


async def _embedding_step(embedder, items: list[Item], *, llm, budget: Budget, framework: str, role_and_level: str, prior: dict[str, Any] | None,
                          repo_aliases: dict[str, str], new_ids: set[str] | None) -> tuple[dict[str, list[float]], list[str]]:
    """Embeddings are optional grouping help, so they never take the calls the required chat work still needs. They are sent only when (a) there is new
    evidence to group (a backfill, or a week with new items), and (b) one pass over ALL items fits in what is left of the run's model budget after the
    pending work. Otherwise nothing is sent and the notes say so; grouping then uses keywords and the saved project links. There is no vector cache:
    a saved item keeps only a truncated text, so an old vector could not be matched to its input."""
    if new_ids is not None and not new_ids:
        return {}, ["embeddings not re-run: there is no new evidence since the last run, so grouping used keywords and the saved project links"]
    need = embed.batches_needed(len(items))
    reserve, parts = _pending_chat_calls(items, llm=llm, budget=budget, framework=framework, role_and_level=role_and_level, prior=prior, repo_aliases=repo_aliases)
    room = budget.room(reserve)
    if need > embed.MAX_BATCHES or need > room:
        pend = ", ".join(f"{k} {v}" for k, v in parts.items() if v) or "none"
        return {}, [f"embeddings not used: grouping all {len(items)} items needs {need} request(s) (a pass is limited to {embed.MAX_BATCHES}), but only {room} of this run's "
                    f"{budget.max_calls} model calls are free after the {reserve} kept for work still pending (pending: {pend}; reserve {ladder.MAP_RESERVE}). No embedding request "
                    "was sent; grouping used keywords and the saved project links"]
    return await embedder.embed(items, budget=budget, reserve=reserve)


async def build(items: list[Item], *, username: str, names: set[str], llm, budget: Budget, framework: str, role_and_level: str,
                prior: dict[str, Any] | None, repo_aliases: dict[str, str] | None = None, embedder=None, team_changes: list[tuple[str, str]] | None = None, reuse_names: bool = False,
                new_ids: set[str] | None = None) -> dict[str, Any]:
    by_id = {i.id: i for i in items}
    dense, enotes = ({}, [])
    if embedder is not None and items:
        dense, enotes = await _embedding_step(embedder, items, llm=llm, budget=budget, framework=framework, role_and_level=role_and_level, prior=prior,
                                              repo_aliases=repo_aliases or {}, new_ids=new_ids)
    clusters = linking.link(items, repo_aliases or {}, dense=dense or None)
    links = {i: r for c in clusters.values() for i, r in c["links"].items()}
    if reuse_names:  # a saved item that re-linking now drops (its stored text is shorter) goes back to the project it was in
        home: dict[str, str] = {}
        for pid, c in clusters.items():
            votes = Counter(by_id[i].meta["saved_project"] for i in c["item_ids"] if by_id[i].meta.get("saved_project"))
            if votes:
                home.setdefault(votes.most_common(1)[0][0], pid)
        placed = {iid for c in clusters.values() if len(c["item_ids"]) >= 2 for iid in c["item_ids"]}
        for it in items:
            sp = it.meta.get("saved_project")
            if sp and it.id not in placed and sp in home:
                for c in clusters.values():  # leave the one-item group it formed alone
                    if it.id in c["item_ids"]:
                        c["item_ids"] = [x for x in c["item_ids"] if x != it.id]
                clusters[home[sp]]["item_ids"].append(it.id)
    project_of = {iid: pid for pid, c in clusters.items() for iid in c["item_ids"]}
    notes: list[str] = list(enotes)
    if embedder is not None and dense:
        st = getattr(embedder, "last", {}) or {}
        notes.append(f"topic grouping used embeddings: all {len(items)} items in {st.get('attempts', '?')} request(s), {st.get('failed_attempts', 0)} failed, each charged to the run's model budget, plus keywords")
    prior_ver = (prior or {}).get("verdicts", {})

    # invisible work / thread verdicts (model only for threads we have no verdict for)
    cand = [i for i in items if i.type == "thread" and i.meta.get("mine") and not analysis.has_strong_link(i, links) and i.id not in prior_ver]
    verdicts, n1 = await analysis.classify_threads_with_model(llm, budget, cand, names)
    notes += n1
    allv = {k: analysis.ThreadVerdict(**v) for k, v in prior_ver.items()}
    allv.update(verdicts)
    invisible = analysis.find_invisible(items, links, names, allv)
    dcands = [i for i in items if analysis.is_decision_candidate(i)]
    prior_dv = (prior or {}).get("dverdicts", {})  # saved decision wording: the same sentence in every mode, and no repeat model call
    dverd = {k: analysis.DecisionVerdict(**v) for k, v in prior_dv.items() if k in {c.id for c in dcands}}
    new_dv, n0 = await analysis.classify_decisions_with_model(llm, budget, [c for c in dcands if c.id not in dverd])
    dverd.update(new_dv)
    notes += n0
    decisions = analysis.find_decisions(items, names, username, dverd)

    # projects (only those with real work, 2+ items), cards by model or rules
    # a project needs 2+ linked items and at least one item that is real tracked work (PR, ticket, commit, review or doc).
    # Slack threads that only resemble each other are chatter, not a project.
    def is_project(c: dict) -> bool:
        real = [i for i in c["item_ids"] if by_id[i].type in {"pr", "ticket", "commit"} and not (by_id[i].type in {"commit", "pr"} and TRIVIAL_TITLE.search(by_id[i].title))]
        return len(c["item_ids"]) >= MIN_PROJECT_ITEMS and bool(real)

    base = [analysis.baseline_project_card(pid, c["item_ids"], by_id, username) for pid, c in sorted(clusters.items()) if is_project(c)]
    # One name, goal and outcome per project in every mode: the backfill saves them with the project's item keys; a later run gives each
    # project the saved card it shares the most items with (one saved card per project, any shared item counts), and only projects with
    # no saved card go to the model.
    saved = list((prior or {}).get("cards", [])) if reuse_names else []
    pairs = sorted(((len({id_key(e) for e in b["evidence"]} & set(s_.get("ev", []))), bi, si) for bi, b in enumerate(base) for si, s_ in enumerate(saved)), reverse=True)
    match: dict[int, dict] = {}
    used: set[int] = set()
    for n_, bi, si in pairs:
        if n_ >= 1 and bi not in match and si not in used:
            match[bi] = saved[si]
            used.add(si)
    unchanged = {bi for bi, sv in match.items() if {id_key(e) for e in base[bi]["evidence"]} <= set(sv.get("ev", []))}  # nothing new in this project since it was saved
    todo = [b for bi, b in enumerate(base) if bi not in unchanged]  # a project with new items gets a fresh outcome, but keeps its saved name and goal
    cards_new, n2 = await analysis.project_cards_with_model(llm, budget, todo, by_id, username) if todo else ([], [])
    notes += n2
    by_pid = {c["project_id"]: c for c in cards_new}
    cards = []
    for bi, b in enumerate(base):
        card = dict(by_pid.get(b["project_id"], b))
        if bi in match:
            sv = match[bi]
            card["name"] = sv["name"]
            for fld in ("goal", "outcome") if bi in unchanged else ("goal",):
                if sv.get(fld):
                    card[fld] = sv[fld]
        cards.append(card)
    card_store = [{"ev": sorted({id_key(e) for e in c["evidence"]})[:60], "name": c["name"], "goal": c.get("goal", "")[:200], "outcome": c.get("outcome", "")[:240]} for c in cards]
    for card in cards:
        prs = [by_id[e] for e in card["evidence"] if by_id[e].type == "pr"]
        card["co_contributors"] = [n for n in {p for e in card["evidence"] for p in ([by_id[e].author] + by_id[e].co_authors)} if n and n.lower() != username.lower() and n not in names and not outputs_id(n)][:6]
        if team_changes:  # team change mid-period: the project's items grouped by the team in force, with the change date
            split = linking.split_by_team([by_id[x] for x in clusters[card["project_id"]]["item_ids"]], team_changes)
            since = {t: d for d, t in team_changes}
            card["teams"] = [{"team": t or "before the first change", "since": since.get(t, ""), "items": len(ids_)} for t, ids_ in sorted(split.items(), key=lambda kv: since.get(kv[0], ""))]
        card["counts"] = {t: sum(1 for e in clusters[card["project_id"]]["item_ids"] if by_id[e].type == t) for t in ("pr", "review", "ticket", "thread", "doc")}
    stats = metrics.pr_stats(items)
    improv = metrics.improvement_areas(items)
    skills = analysis.map_skills(items)

    lab = {v["thread"]: v["label"] for v in invisible}
    for i in items:
        if i.type == "thread":
            i.meta["label"] = lab.get(i.id, "")
    gaps: list[dict] = []
    exps: list[Expectation] = parse_framework(framework, role_and_level) if framework.strip() else []
    fw_status, fw_cache, fw_cache_new = ("absent" if not framework.strip() else "bullets"), None, False
    if framework.strip() and not exps:  # prose: the model reads it once, the result is cached by the framework's hash
        h = ladder.framework_hash(framework, role_and_level)
        saved_fw = (prior or {}).get("framework") or {}
        if saved_fw.get("hash") == h and saved_fw.get("exps"):
            exps, fw_status, fw_cache = [Expectation(**e) for e in saved_fw["exps"]], "cached", saved_fw
            notes.append(f"review framework: {len(exps)} expectations reused from the saved extraction (no model call)")
        elif llm is None:
            fw_status = "unparsed"
            notes.append("review framework: it has no bullet or numbered lines and no model was available to read it, so evidence gaps were NOT run")
        else:
            exps, why, rejected = await ladder.extract_with_model(llm, budget, framework, role_and_level)
            if exps:
                fw_status, fw_cache, fw_cache_new = "model", {"hash": h, "exps": [{"id": e.id, "level": e.level, "text": e.text, "quote": e.quote} for e in exps]}, True
                notes.append(f"review framework: it was prose, so the model extracted {len(exps)} expectations, each with a quote from the framework; they are saved and reused while the framework is unchanged"
                             + (f" ({rejected} extracted line(s) had no quote in the framework and were dropped)" if rejected else "") + (f"; {why}" if why else ""))
            else:
                fw_status = "failed"
                notes.append(f"review framework: it was provided but its expectations could not be extracted - {why}. Evidence gaps were NOT run")
    mp = None
    if exps:  # the model picks which evidence shows each expectation; code checks the ids and counts
        mp = await ladder.map_evidence_with_model(llm, budget, exps, items, (prior or {}).get("mapping"))
        gaps = statuses(exps, mp.evidence, set(by_id), incomplete=mp.incomplete)
        n_un = sum(1 for g in gaps if g["status"] == "unavailable")
        if mp.reused and not mp.new_calls:
            notes.append(f"evidence mapping: {mp.reused} evidence item(s) answered from the saved mapping (no model call)")
        elif mp.new_calls:
            notes.append(f"evidence mapping: the model compared {len(exps)} expectation(s) with the evidence in {mp.new_calls} request(s); {mp.reused} item(s) were reused from the saved mapping")
        if mp.incomplete:
            notes.append(f"evidence mapping UNAVAILABLE for {n_un} expectation(s): " + "; ".join(mp.reasons or ["part of the evidence was not mapped"]) + ". This is not a finding that evidence is missing - those expectations are shown as unavailable")
        elif mp.reasons:
            notes.append("evidence mapping: " + "; ".join(mp.reasons))
    elif fw_status == "absent":
        notes.append("no review framework given: evidence gaps skipped; a framework would enable them")

    projects_clean = []
    for c in cards:
        projects_clean.append(c)
    team_ev = [i for i in items if i.meta.get("confidential")]
    return {
        "projects": projects_clean, "invisible": invisible, "decisions": decisions, "skills": skills, "improvements": improv,
        "gaps": gaps, "stats": stats, "notes": notes, "project_of": {i: p for i, p in project_of.items() if p in {c["project_id"] for c in projects_clean}},
        "verdicts": {k: v.model_dump() for k, v in allv.items()}, "card_store": card_store, "dverdicts": {k: v.model_dump() for k, v in dverd.items()},
        "confidential": sorted(confidential_ids(items)), "framework_status": fw_status, "framework_cache": fw_cache, "framework_cache_new": fw_cache_new,
        "mapping_cache": mp.cache if mp is not None else None, "mapping_cache_new": bool(mp is not None and mp.new_calls),
        "top": [{"id": i.id, "title": i.title, "lines": metrics.contribution_size(i)} for i in metrics.top_contributions(items, 8)],
    }
