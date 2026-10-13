"""Raw source bundle -> clean, deduped, filtered Items. Pure; the graph and the tests both call this."""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any

from guards import ToolLog, channels_to_read, drop_unconsented, parse_channels
from models import Item, Skipped
import normalize as N


@dataclass
class Config:
    username: str
    repos: list[str]
    jira_site: str = ""
    jira_projects: list[str] = field(default_factory=list)
    slack_channels: list[str] = field(default_factory=list)
    include_dms: bool = False
    slack_user_ids: set[str] = field(default_factory=set)
    name_hints: list[str] = field(default_factory=list)  # display names / emails for Jira + docs
    repo_aliases: dict[str, str] = field(default_factory=dict)
    docs_sources: list[str] = field(default_factory=list)


@dataclass
class Ingest:
    items: list[Item]
    skipped: list[Skipped]
    duplicates: int
    dm_dropped: int
    not_found_channels: list[str]
    counts: dict[str, int]


def infer_merged(prs: list[dict], commits: list[dict]) -> None:
    """Closed PRs with no merge time from the gateway: confirm the merge from the repo's own commits
    (merge_commit_sha seen, 'Merge pull request #N', or 'title (#N)'). Unconfirmed ones stay unmerged."""
    for p in prs:
        if N.pr_merged_at(p) or str(p.get("state", "")).lower() != "closed":
            continue
        repo, num = p.get("_repo", ""), p.get("number")
        sha = p.get("merge_commit_sha") or (p.get("pull_request") or {}).get("merge_commit_sha") or ""
        for c in commits:
            if c.get("_repo") != repo:
                continue
            msg = ((c.get("commit") or {}).get("message") or c.get("message") or "").splitlines()
            first = msg[0] if msg else ""
            hit = bool(sha and c.get("sha") == sha) or re.match(rf"Merge pull request #{num}\b", first) or re.search(rf"\(#{num}\)\s*$", first)
            if hit:
                when = (c.get("commit") or {}).get("committer", {}).get("date") or (c.get("commit") or {}).get("author", {}).get("date") or p.get("closed_at")
                p["merged_at"], p["_merged_basis"] = when, "merge commit found in repo history"
                break


def _pr_head(p: dict) -> str:
    return (p.get("head") or {}).get("ref", "") if isinstance(p.get("head"), dict) else str(p.get("head") or "")


def covered_by_pr(c: Item, prs: list[dict], canon: Any = None, pkeys: dict[int, list[str]] | None = None) -> bool:
    """A commit that is part of one of the user's PRs, on relation evidence only: the commit's SHA is in the PR's own
    commit list; the same ticket key inside the PR's life (old keys resolved through the tickets' alias records; PR keys
    include branch-name keys); or only files the PR touched inside its life. A same or similar title never merges a
    commit - same titles can be independent work, so an uncertain commit stays its own row."""
    cd = N.parse_ts(c.date)
    cpaths = set(c.meta.get("paths") or [])
    csha = str(c.meta.get("sha") or "")
    canon = canon or (lambda k: k)
    ckeys = {canon(k) for k in c.keys}
    for i, p in enumerate(prs):
        if p.get("_repo") != c.meta.get("repo") or N._login(p.get("user")).lower() != str(c.author).lower():
            continue
        if csha and (csha in set(p.get("_commit_shas") or []) or csha == str(p.get("merge_commit_sha") or "")):
            return True  # a squash/merge commit is the PR itself: its sha is the PR's merge_commit_sha
        start, end = N.parse_ts(p.get("created_at")), N.parse_ts(N.pr_merged_at(p) or p.get("closed_at"))
        if not (cd and start and end and start.timestamp() - 7 * 86400 <= cd.timestamp() <= end.timestamp() + 120):
            continue
        if ckeys and pkeys and ckeys & {canon(k) for k in pkeys.get(i, [])}:
            return True
        ppaths = {f.get("filename") for f in (p.get("files") or []) if isinstance(f, dict)}
        if cpaths and ppaths and cpaths <= ppaths:
            return True
    return False


_STOP = set("the a an and or to of in on for is it we i you this that was were be at as with from by are not but so if do did have has had just now then there their they them our can will would should could what when how why which who been into out up down over one two".split())


def _toks(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower()) if w not in _STOP}


def _toks_clean(text: str) -> set[str]:
    body = "\n".join(l.split(": ", 1)[1] if ": " in l[:40] else l for l in text.splitlines())
    return _toks(body)


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


_OPENER = re.compile(r"\?|\b(anyone|any idea|not sure|stuck|can someone|does anyone|how (do|can|should|to)|why (is|does|do)|i keep getting)\b", re.I)


def merge_threads(items: list[Item], gap_minutes: int = 10) -> list[Item]:
    """Flat Slack messages that are one conversation become one item. Word overlap is too weak to find topic boundaries (replies often
    share no words with the question), so boundaries come from structure instead. A message STARTS a new conversation when it:
    is a question or help request, or arrives more than `gap_minutes` after the last one, or has ticket keys when the group has none
    (or the reverse, or different keys), or comes from a different channel / DM-ness / person. Anything else continues the
    conversation, provided it is within 2 minutes or shares a word with it. When unsure the messages stay separate."""
    from datetime import datetime

    def when(i: Item) -> float:
        return datetime.fromisoformat(i.date.replace("Z", "+00:00")).timestamp()
    threads = sorted((i for i in items if i.type == "thread"), key=lambda i: (i.meta.get("channel_id", ""), i.date, i.id))
    rest = [i for i in items if i.type != "thread"]
    groups: list[list[Item]] = []
    for it in threads:
        last = groups[-1][-1] if groups else None
        if last and last.meta.get("channel_id") == it.meta.get("channel_id") and bool(last.meta.get("is_dm")) == bool(it.meta.get("is_dm")):
            gap_min = (when(it) - when(last)) / 60
            same_people = bool(set(last.meta.get("participants", [])) & set(it.meta.get("participants", [])))
            ka = set().union(*(set(x.keys) for x in groups[-1]))
            kb = set(it.keys)
            keys_ok = (not ka and not kb) or bool(ka & kb)
            body = "\n".join(l.split(": ", 1)[1] if ": " in l[:40] else l for l in it.text.splitlines())
            opener = bool(_OPENER.search(body))
            shared = {_stem(w) for w in _toks_clean(last.text)} & {_stem(w) for w in _toks_clean(it.text)}
            if gap_min <= gap_minutes and same_people and keys_ok and not opener and (gap_min <= 2 or shared):
                groups[-1].append(it)
                continue
        groups.append([it])
    merged: list[Item] = []
    for g in groups:
        if len(g) == 1:
            merged.append(g[0])
            continue
        a = g[0]
        a.text = "\n".join(x.text for x in g)[:4000]
        a.keys = sorted({k for x in g for k in x.keys})
        a.meta = {**a.meta, "messages": sum(int(x.meta.get("messages", 1)) for x in g), "my_messages": sum(int(x.meta.get("my_messages", 0)) for x in g),
                  "participants": sorted({p for x in g for p in x.meta.get("participants", [])}), "merged_ids": [x.id for x in g[1:]]}
        merged.append(a)
    return rest + merged


def ingest(raw: dict[str, Any], cfg: Config, log: ToolLog | None = None) -> Ingest:
    items: list[Item] = []
    skipped: list[Skipped] = []

    def take(x: Item | Skipped) -> None:
        (items if isinstance(x, Item) else skipped).append(x)

    prs = raw.get("github", {}).get("prs", [])
    reviews = raw.get("github", {}).get("reviews", {})
    pr_by_key: dict[str, dict] = {}
    infer_merged(prs, raw.get("github", {}).get("commits", []))
    for p in prs:
        repo = cfg.repo_aliases.get(p.get("_repo", ""), p.get("_repo", ""))
        pr_by_key[f"{p.get('_repo','')}#{p.get('number')}"] = p
        authored_by_me = N._login(p.get("user")).lower() == cfg.username.lower()
        if authored_by_me:
            take(N.normalize_pr(p, repo, cfg.username, cfg.jira_projects))
    for key, revs in reviews.items():
        repo, num = key.rsplit("#", 1)
        pr = pr_by_key.get(key, {})
        for rv in revs:
            reviewer = N._login(rv.get("user"))
            pr_author = N._login(pr.get("user"))
            # keep reviews I wrote, and reviews I received on my PRs (feedback patterns); drop everyone else's
            if reviewer.lower() == cfg.username.lower() and pr_author.lower() == cfg.username.lower():
                skipped.append(Skipped(f"gh:review:{repo}#{num}:{rv.get('id')}", "own comment on own PR (not a review given)"))
                continue
            if reviewer.lower() == cfg.username.lower() or pr_author.lower() == cfg.username.lower():
                take(N.normalize_review(rv, cfg.repo_aliases.get(repo, repo), int(num), pr_author, pr.get("html_url", ""), cfg.username, cfg.jira_projects))
    alias: dict[str, str] = {}  # moved tickets: the old key in a branch or commit message resolves to the current key
    for t in raw.get("jira", []):
        x = N.normalize_ticket(t, cfg.jira_site, cfg.name_hints)
        if isinstance(x, Item) and x.keys:
            for old in x.meta.get("aliases", []):
                alias.setdefault(str(old), x.keys[0])
        take(x)

    def canon(k: str) -> str:
        return alias.get(k, k)

    pr_keys = {i: N.ticket_keys(_pr_head(p), p.get("title") or "", p.get("body") or "", projects=cfg.jira_projects) for i, p in enumerate(prs)}
    pr_nums = {(p.get("_repo", ""), str(p.get("number"))) for p in prs}
    for c in raw.get("github", {}).get("commits", []):
        x = N.normalize_commit(c, c.get("_repo", ""), cfg.username, cfg.jira_projects)
        if isinstance(x, Item):
            first = x.title
            m = re.search(r"\(#(\d+)\)\s*$", first) or re.match(r"Merge pull request #(\d+)", first)
            if not x.meta.get("mine"):
                x = Skipped(x.id, "commit by someone else")
            elif first.startswith("Merge ") or (m and (c.get("_repo", ""), m.group(1)) in pr_nums):
                x = Skipped(x.id, "merge commit or commit already counted through its PR")
            elif covered_by_pr(x, prs, canon, pr_keys):
                x = Skipped(x.id, "commit already counted through its PR (its SHA is one of the PR's commits, a shared ticket key, or the same files, inside the PR's dates)")
        take(x)
    gap_burst = 600.0
    channels = raw.get("slack", {}).get("channels", [])
    picked, missing = channels_to_read(channels, cfg.slack_channels, cfg.include_dms, log)
    allowed = {c["id"] for c in picked}
    names = {c["id"]: c["name"] for c in channels}
    dm_ids = {c["id"] for c in channels if c.get("is_im") or c.get("is_mpim")}
    for th in raw.get("slack", {}).get("threads", []):
        if th["channel_id"] not in allowed:
            skipped.append(Skipped(f"slack:{th['channel_id']}", "channel not chosen"))
            continue
        if log is not None:
            log.add("slack.read_history", channel=th["channel_id"], is_dm=th["channel_id"] in dm_ids)
        uids = cfg.slack_user_ids | set(raw.get("slack", {}).get("my_user_ids", []))
        base = raw.get("slack", {}).get("permalink_base", "")
        msgs = th["messages"]
        tss = sorted(float(m.get("ts", 0) or 0) for m in msgs)
        # a "thread" whose messages all fall within a few minutes is chat posted in a burst that opens 3 or more separate questions; each message goes to merge_threads,
        # which splits it by topic. Longer real threads stay one item.
        openers = sum(1 for m in msgs if _OPENER.search(str(m.get("text", ""))))
        parts = [[m] for m in msgs] if len(msgs) >= 2 and (tss[-1] - tss[0]) <= gap_burst and openers >= 3 else [msgs]
        for part in parts:
            x = N.normalize_thread(names[th["channel_id"]], th["channel_id"], part, uids, base)
            if isinstance(x, Item):
                x.meta["is_dm"] = th["channel_id"] in dm_ids
            take(x)
    for d in raw.get("docs", []):
        take(N.normalize_doc(d, d.get("_source", "confluence"), cfg.name_hints, cfg.jira_projects))

    items = [i for i in items if i.date]
    items, dm_dropped = drop_unconsented(items, cfg.include_dms)
    items, dups = N.dedupe(items)
    items = merge_threads(items)
    for i in items:  # nothing disappears silently: messages folded into another item are listed with the reason
        for mid in i.meta.get("merged_ids", []):
            skipped.append(Skipped(mid, "message folded into the same Slack conversation"))
    N.mark_reverted([i for i in items if i.type == "pr"])
    N.mark_reverted_commits([i for i in items if i.type == "commit"])
    counts: dict[str, int] = {}
    for i in items:
        counts[i.type] = counts.get(i.type, 0) + 1
    return Ingest(items, skipped, dups, dm_dropped, missing, counts)

