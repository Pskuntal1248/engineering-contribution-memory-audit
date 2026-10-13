"""Link items into projects. Strong links (ticket key, URL, PR number, shared
branch) are exact; topic/time closeness only attaches loose items (Slack threads,
docs) to an existing cluster and never merges two clusters."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import timedelta

from models import Item
from normalize import parse_ts

GENERIC = set("add fix fixes fixed initial commit merge bump chore refactor feat docs test tests new set setup minor wip misc small typo".split())
STOP = set("update status done thanks work ok see follow ticket part change the a an and or of to in on for with is it this that we i you be at as by from are was were will can not but have has had do did so if then also just now get got use used using into out up".split())


class UnionFind:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic: smaller id becomes the root
            lo, hi = sorted((ra, rb))
            self.p[hi] = lo


def _stem(t: str) -> str:
    for suf in ("ies", "ing", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def tokens(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z][a-z0-9_]{2,}", (text or "").lower()) if t not in STOP and t not in GENERIC]


def tfidf_vectors(items: list[Item]) -> dict[str, dict[str, float]]:
    docs = {it.id: Counter(tokens(it.title + " " + it.text[:1500] + " " + " ".join(it.meta.get("paths", [])))) for it in items}
    df: Counter[str] = Counter()
    for c in docs.values():
        df.update(c.keys())
    n = max(len(items), 1)
    vecs: dict[str, dict[str, float]] = {}
    for iid, c in docs.items():
        v = {t: (1 + math.log(f)) * math.log(1 + n / df[t]) for t, f in c.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs[iid] = {t: x / norm for t, x in v.items()}
    return vecs


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def key_aliases(items: list[Item]) -> dict[str, str]:
    """Moved tickets (PAY-1 -> WEB-9) and renamed repos resolve to one canonical key."""
    alias: dict[str, str] = {}
    for it in items:
        if it.source == "jira":
            for old in it.meta.get("aliases", []):
                alias[old] = it.keys[0]
    return alias


def blend(sparse: dict[str, dict[str, float]], dense: dict[str, list[float]] | None, weight: float = 0.5) -> dict[str, dict[str, float]]:
    """Topic vector = sqrt(1-w) * keyword vector joined with sqrt(w) * embedding. Both parts are unit length, so the
    cosine of two blended vectors is (1-w) * keyword cosine + w * embedding cosine, on the same scale as before.
    Items with no embedding keep the keyword vector only."""
    if not dense:
        return sparse
    a, b = math.sqrt(1 - weight), math.sqrt(weight)
    out: dict[str, dict[str, float]] = {}
    for iid, v in sparse.items():
        e = dense.get(iid)
        if e is None:
            out[iid] = v
            continue
        m = {t: x * a for t, x in v.items()}
        m.update({f"\x00e{i}": x * b for i, x in enumerate(e)})
        out[iid] = m
    return out


def link(items: list[Item], repo_aliases: dict[str, str] | None = None, topic_threshold: float = 0.28,
         window_days: int = 10, project_threshold: float = 0.22, dense: dict[str, list[float]] | None = None) -> dict[str, dict]:
    """Return {project_id: {"item_ids": [...], "links": {item_id: [reason,...]}}}.
    Project ids are the root item id, so they are stable between runs."""
    repo_aliases = repo_aliases or {}
    by_id = {i.id: i for i in items}
    alias = key_aliases(items)
    uf = UnionFind()
    why: dict[str, list[str]] = defaultdict(list)
    for i in items:
        uf.find(i.id)

    def canon_key(k: str) -> str:
        return alias.get(k, k)

    # 1. ticket keys: every item that mentions a key links to the ticket and to each other
    # Work items (ticket, PR, commit, review) that share a key are one unit. A Slack thread or doc that names keys from several
    # tickets only joins the unit of its FIRST key: it never bridges two units (a status thread must not merge PAY and AUTH).
    holders: dict[str, list[str]] = defaultdict(list)
    for i in items:
        if i.type in {"thread", "doc"}:
            continue
        for k in set(canon_key(k) for k in i.keys):
            holders[k].append(i.id)
    for k, ids in holders.items():
        for other in ids[1:]:
            uf.union(ids[0], other)
            why[other].append(f"ticket key {k}")
            why[ids[0]].append(f"ticket key {k}")
    for i in items:
        if i.type in {"thread", "doc"} and i.keys:
            first = canon_key(i.keys[0])
            if holders.get(first):
                uf.union(holders[first][0], i.id)
                why[i.id].append(f"ticket key {first}")

    # 2. same branch, same PR number (PR <-> its reviews/commits)
    pr_ids: dict[tuple[str, int], str] = {}
    for i in items:
        if i.type == "pr":
            pr_ids[(repo_aliases.get(i.meta["repo"], i.meta["repo"]), i.meta["number"])] = i.id
    for i in items:
        if i.type == "review":
            pid = pr_ids.get((repo_aliases.get(i.meta["repo"], i.meta["repo"]), i.meta["number"]))
            if pid:
                uf.union(pid, i.id)
                why[i.id].append("review on linked PR")
        text_refs = re.findall(r"(?:^|[\s(])#(\d{1,6})\b", i.text or "")
        if i.type in {"commit", "thread", "doc", "ticket"} and i.meta.get("repo"):
            for n in text_refs:
                pid = pr_ids.get((repo_aliases.get(i.meta["repo"], i.meta["repo"]), int(n)))
                if pid:
                    uf.union(pid, i.id)
                    why[i.id].append(f"mentions PR #{n}")
    # 3. URL cross references (links shared in Slack, docs linked from tickets)
    url_owner = {i.url: i.id for i in items if i.url}
    for i in items:
        for u in list(i.refs):
            owner = url_owner.get(u)
            if owner and owner != i.id:
                uf.union(owner, i.id)
                why[i.id].append(f"links to {u}")
    # PR body "Fixes #N" style references to PR numbers across repos handled above; revert pairs:
    for i in items:
        if i.type == "pr" and i.meta.get("reverted_by") in by_id:
            uf.union(i.id, i.meta["reverted_by"])
            why[i.id].append("reverted by linked PR")

    clusters: dict[str, list[str]] = defaultdict(list)
    for i in items:
        clusters[uf.find(i.id)].append(i.id)

    # 4. loose items (no strong link) attach to the best nearby cluster by topic. Never merges clusters.
    vecs = blend(tfidf_vectors(items), dense)
    multi = {r: ids for r, ids in clusters.items() if len(ids) > 1}
    centroid: dict[str, dict[str, float]] = {}
    for r, ids in multi.items():
        c: Counter[str] = Counter()
        for iid in ids:
            c.update(vecs[iid])
        norm = math.sqrt(sum(x * x for x in c.values())) or 1.0
        centroid[r] = {t: x / norm for t, x in c.items()}
    span = {r: (min(parse_ts(by_id[i].date) for i in ids), max(parse_ts(by_id[i].date) for i in ids)) for r, ids in multi.items()}
    for r, ids in list(clusters.items()):
        if len(ids) != 1:
            continue
        it = by_id[ids[0]]
        if it.type not in {"thread", "doc", "commit"}:
            continue
        d = parse_ts(it.date)
        best, score = None, 0.0
        for mr, cen in centroid.items():
            lo, hi = span[mr]
            if d is None or d < lo - timedelta(days=window_days) or d > hi + timedelta(days=window_days):
                continue
            s = cosine(vecs[it.id], cen)
            if s > score or (s == score and best is not None and mr < best):
                best, score = mr, s
        if best and score >= topic_threshold:
            clusters[best].append(it.id)
            clusters[r] = []
            why[it.id].append(f"close in time and topic (similarity {score:.2f})")

    # 5. group work units into projects by topic (what the spec calls grouping by embeddings; here TF-IDF
    #    cosine, deterministic). Leader clustering: biggest units seed projects, others join the closest
    #    seed above the threshold, so two projects are never chained together through a weak bridge.
    units = {r: ids for r, ids in clusters.items() if ids}
    cvec = {}
    for r, ids in units.items():
        c: Counter[str] = Counter()
        for iid in ids:
            c.update(vecs[iid])
        norm = math.sqrt(sum(x * x for x in c.values())) or 1.0
        cvec[r] = {t: x / norm for t, x in c.items()}
    def prefixes(ids: list[str]) -> set[str]:
        return {canon_key(k).split("-")[0] for iid in ids if by_id[iid].type not in {"thread", "doc"} for k in by_id[iid].keys if "-" in k}

    pre = {r: prefixes(ids) for r, ids in units.items()}

    def compatible(a: set[str], b: set[str]) -> bool:
        """Units that belong to different Jira projects (PAY vs AUTH) are never folded into one project by topic similarity."""
        return not a or not b or bool(a & b)

    order = sorted(units, key=lambda r: (-len(units[r]), r))
    seeds: list[str] = []
    members: dict[str, list[str]] = {}
    sums: dict[str, Counter] = {}
    for r in order:
        best, score = None, 0.0
        for sd in seeds:
            if not compatible(pre[r], set().union(*(pre[m] for m in members[sd]))):
                continue
            sc = cosine(cvec[r], cvec[sd])
            if sc > score or (sc == score and best is not None and sd < best):
                best, score = sd, sc
        if best is not None and score >= project_threshold:
            members[best].append(r)
            for iid in units[r]:
                why[iid].append(f"same topic as project (similarity {score:.2f})") if len(units[r]) == 1 else None
        else:
            seeds.append(r)
            members[r] = [r]
    # refine: recompute centroids and reassign every unit (3 rounds), then fold near-identical groups
    def centroids(groups: dict[str, list[str]]) -> dict[str, dict[str, float]]:
        out = {}
        for g, rs in groups.items():
            c: Counter[str] = Counter()
            for r in rs:
                for iid in units[r]:
                    c.update(vecs[iid])
            norm = math.sqrt(sum(x * x for x in c.values())) or 1.0
            out[g] = {t: x / norm for t, x in c.items()}
        return out

    for _ in range(3):
        cen = centroids(members)
        new: dict[str, list[str]] = {g: [] for g in members}
        for r in order:
            best, score = min(members), -1.0
            for g in sorted(cen):
                if not compatible(pre[r], set().union(*(pre[m] for m in members[g]))):
                    continue
                sc = cosine(cvec[r], cen[g])
                if sc > score:
                    best, score = g, sc
            if score < 0:  # nothing compatible: stay where it was
                best = next((g for g, rs in members.items() if r in rs), best)
            new[best].append(r)
        members = {g: rs for g, rs in new.items() if rs}
    cen = centroids(members)
    for a in sorted(members, key=lambda g: (-len(members[g]), g)):
        if a not in members:
            continue
        for b in sorted(members):
            if b != a and b in members and cosine(cen[a], cen[b]) >= 0.6 and compatible(
                    set().union(*(pre[m] for m in members[a])), set().union(*(pre[m] for m in members[b]))):
                members[a] += members.pop(b)
    # small leftovers from the same Jira project (a few PR+ticket pairs, 6 items or fewer per group) are one project, not several:
    # PAY-1, PAY-2 and PAY-3 are PAY work. Big groups are never folded, so separate projects inside one Jira project stay separate.
    def gsize(g: str) -> int:
        return sum(len(units[r]) for r in members[g])

    def gpre(g: str) -> frozenset:
        return frozenset(set().union(*(pre[m] for m in members[g])))
    by_pre: dict[frozenset, list[str]] = defaultdict(list)
    for g in sorted(members):
        if gpre(g) and gsize(g) <= 6:
            by_pre[gpre(g)].append(g)
    for gs in by_pre.values():
        for other in gs[1:]:
            members[gs[0]] += members.pop(other)
    clusters = defaultdict(list)
    for sd, rs in members.items():
        for r in rs:
            clusters[sd].extend(units[r])

    out: dict[str, dict] = {}
    for r, ids in clusters.items():
        if ids:
            out[r] = {"item_ids": sorted(ids, key=lambda x: (by_id[x].date, x)),
                      "links": {i: sorted(set(why.get(i, []))) for i in ids}}
    return out


def split_by_team(project_items: list[Item], team_changes: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Team change mid-period: group a project's items by the team in force on the item's date.
    team_changes is [(from_date_iso, team_name), ...] sorted."""
    out: dict[str, list[str]] = defaultdict(list)
    for it in project_items:
        team = ""
        for d, t in team_changes:
            if it.date >= d:
                team = t
        out[team].append(it.id)
    return dict(out)
