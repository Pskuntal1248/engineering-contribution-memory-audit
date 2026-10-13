#!/usr/bin/env python3
"""One command end-to-end check of the UC-10 agent against the user's real connected tools.

Runs the first-run backfill, then every on-demand mode, saves each output to out/<mode>.md (and out/<mode>.json,
out/<mode>.log) and checks them. Prints one PASS/FAIL table and only the failing lines. Reads only; nothing is
written to any source. Existing memory is moved to .gravity/backup-<time>/ (not deleted) so the backfill is a real first run.

Usage:  python3 run_all.py            (optional: --check-only  re-checks the files already in out/)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT, GRAV = ROOT / "out", ROOT / ".gravity"
BASE = {
    "github_username": "Pskuntal1248", "repos_or_org": "Pskuntal1248/uc10-synthetic-test-data",
    "jira_site": "https://pskuntal2005.atlassian.net", "jira_projects": "PAY, SRCH, AUTH", "slack_channels": "#uc10-test",
    "include_dms": "No", "docs_source": "Confluence + Notion", "role_and_level": "Software Engineer",
    "timezone": "Asia/Kolkata", "display_name": "Parth Singh",
}
LADDER = """Software Engineer to Senior Software Engineer expectations:
1. Delivers complex features end to end with little guidance.
2. Writes clear design docs for work that spans several services.
3. Reviews teammates' code and raises the quality bar in reviews.
4. Mentors junior engineers and helps them grow.
5. Owns the reliability of the services they ship, including on-call follow-up.
6. Leads a cross-team technical decision.
7. Communicates status and risks to stakeholders without being asked.
8. Reduces recurring team toil through tooling or process improvements.
"""
# name, extra inputs, limit seconds, limit model calls
RUNS = [
    ("backfill", {"backfill_months": "6"}, 1800, 150),
    ("weekly_summary", {"request": "weekly_summary"}, 300, 25),
    ("one_on_one", {"request": "one_on_one"}, 120, 10),
    ("ask_review", {"ask": "review material for April to September"}, 120, 10),
    ("review_material", {"request": "review_material", "period_from": "2026-09-01", "period_to": "2026-10-31"}, 120, 10),
    ("interview_stories", {"request": "interview_stories"}, 120, 10),
    ("evidence_gaps", {"request": "evidence_gaps", "review_framework": LADDER}, 120, 10),
]
SEVEN = ["Projects", "Problems solved", "Decisions", "Invisible work", "Skills", "Improvement areas", "Evidence gaps"]
EXTRA_OK = {"Other Slack conversations", "What could not be done"}
WEEKLY = ["What changed this week", "Shipped", "In progress", "Decisions", "Problems solved", "Help given"]
ONE = ["Wins", "You may not have seen", "Blockers", "Decisions made", "Decision needing input", "Working on", "For the next level"]
NO_LINK_NEEDED = {"Coverage", "What changed this week", "What could not be done"}
FLAG = "CONFIDENTIAL source"


def run_one(name, extra, limit_s):
    inputs = {**BASE, **extra}
    cmd = ["gravity", "run"]
    for k, v in inputs.items():
        cmd += ["--input", f"{k}={v}"]
    for f in ("last_output.md", "last_result.json", "last_ledger.csv"):
        (GRAV / f).unlink(missing_ok=True)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=limit_s + 300)
        log, code = (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired as e:
        log, code = f"TIMEOUT after {limit_s + 300}s\n{e.stdout or ''}", 124
    wall = time.time() - t0
    (OUT / f"{name}.log").write_text(log, encoding="utf-8")
    for src, dst in (("last_output.md", f"{name}.md"), ("last_result.json", f"{name}.json"), ("last_ledger.csv", f"{name}.csv")):
        if (GRAV / src).is_file():
            shutil.copy(GRAV / src, OUT / dst)
    return wall, code


def sections_of(md):
    """[(level, heading, body)] for every heading line, body up to the next heading."""
    parts = re.split(r"(?m)^(#{2,4}) (.+)$", md)
    return [(len(parts[i]), parts[i + 1].strip(), parts[i + 2]) for i in range(1, len(parts) - 2, 3)]


def blocks_of(body):
    """Item blocks: a paragraph starting with a bold title and the paragraphs after it, up to the next bold title."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    out, cur = [], None
    for p in paras:
        if re.match(r"\*\*[^*]+\*\*", p):
            cur = [p]
            out.append(cur)
        elif cur is not None:
            cur.append(p)
    return out


def check_mode(name, md, res, wall, code, log, limit_s, limit_calls):
    fails, ok = [], {}

    def chk(label, cond, detail=""):
        ok[label] = bool(cond)
        if not cond:
            fails.append(f"[{name}] {label}: {detail}")

    chk("ran, output saved", code == 0 and bool(md), f"exit {code}, output {'missing' if not md else 'present'}")
    chk("no TaskGroup/traceback", not re.search(r"TaskGroup|Traceback", md + "\n" + log), "; ".join(l.strip()[:140] for l in (md + "\n" + log).splitlines() if re.search(r"TaskGroup|Traceback", l))[:300])
    secs = sections_of(md)
    top = [h for lv, h, _ in secs if lv == 2]
    ledger_ok = sum(1 for h in top if h.startswith(("Item ledger", "Appendix"))) <= 1
    chk("ledger appears once, last", ledger_ok and (not top or top[-1].startswith(("Appendix", "Item ledger"))), f"headings: {top[-2:]}")
    secs_wo_ledger = [(lv, h, b) for lv, h, b in secs if not h.startswith(("Appendix", "Item ledger"))]
    # every item has a link
    nolink = []
    for lv, h, b in secs_wo_ledger:
        if h in NO_LINK_NEEDED:
            continue
        for blk in blocks_of(b):
            txt = "\n".join(blk)
            if len(blk) > 1 and "No items found" not in txt and not re.search(r"https?://|Source: ", txt):
                nolink.append(blk[0][:60])
    chk("every item has a link", not nolink, f"{len(nolink)} without: {nolink[:3]}")
    # repeated title lines
    rep = []
    for lv, h, b in secs_wo_ledger:
        for blk in blocks_of(b):
            t = re.sub(r"\W+", " ", blk[0]).strip().lower()
            if len(blk) > 1 and len(t) > 12 and t[:40] in re.sub(r"\W+", " ", blk[1]).strip().lower()[:100]:
                rep.append(blk[0][:60])
        for blk in blocks_of(b):  # the same line twice inside one item
            seen = set()
            for l in (x.strip() for x in "\n".join(blk).splitlines()):
                if len(l) > 25 and l in seen and not l.startswith(("- [", "[")):
                    rep.append(l[:60])
                seen.add(l)
    chk("no repeated title lines", not rep, f"{len(rep)}: {rep[:3]}")
    # mid-word cuts (heuristic): ellipsis glued into a word, dangling stop word before an ellipsis, unbalanced bracket before one
    cut = re.findall(r"\w(?:\.\.\.|\u2026)\w|\b(?:a|an|the|of|to|and|or|for|with|in|on|by|at|from)(?:\.\.\.|\u2026)|[\[(][^\])\n]{0,80}(?:\.\.\.|\u2026)(?=\s|$)", "\n".join(b for _, _, b in secs_wo_ledger))
    chk("no mid-word cuts", not cut, f"{len(cut)}: {cut[:3]}")
    # limits
    secs_run = res.get("seconds", wall)
    chk(f"runtime < {limit_s // 60} min", max(secs_run, wall) < limit_s, f"{round(max(secs_run, wall))}s")
    chk(f"model calls <= {limit_calls}", res.get("model_calls", 0) <= limit_calls, f"{res.get('model_calls')} calls")

    if name == "backfill":
        chk("7 sections in spec order", [h for h in top if h in SEVEN] == SEVEN and all(h in SEVEN or h in EXTRA_OK or h.startswith(("Appendix", "Item ledger")) for h in top), f"got {top}")
        docs = sum((res.get("docs_items_by_source") or {}).values())
        chk("docs = 3", docs == 3, f"{docs} {res.get('docs_items_by_source')}")
        chk("decisions = 3", res.get("decisions") == 3, str(res.get("decisions")))
        chk("projects = 3", res.get("projects") == 3, str(res.get("projects")))
        chk("PRs shipped = 5", (res.get("stats") or {}).get("prs_shipped") == 5, str((res.get("stats") or {}).get("prs_shipped")))
        chk("confidential flag present", FLAG in md, "no flag text in output")
        chk("no raw commits in profile", not re.search(r"/commit/", "\n".join(b for _, h, b in secs_wo_ledger if h in ("Projects", "Problems solved"))), "commit link in Projects/Problems solved")
    elif name == "weekly_summary":
        quiet = [h for h in top if h == "Nothing new this week"]
        chk("weekly sections match spec", quiet or [h for h in top if h in WEEKLY] == WEEKLY, f"got {top}")
        shipped = "\n".join(b for _, h, b in secs_wo_ledger if h == "Shipped")
        chk("no raw commits in Shipped", "/commit/" not in shipped, "commit link in Shipped")
    elif name in ("one_on_one",):
        chk("1:1 sections match spec", "Wins" in top and all(h in ONE or h.startswith(("Appendix", "Item ledger")) or h in EXTRA_OK for h in top), f"got {top}")
        wins = "\n".join(b for _, h, b in secs_wo_ledger if h == "Wins")
        chk("no raw commits in Wins", "/commit/" not in wins, "commit link in Wins")
        chk("wins not empty", bool(wins.strip()) and "None" not in wins[:6], "Wins section empty")
    elif name in ("ask_review", "review_material"):
        chk("review sections match spec", bool(top) and top[0].startswith("Impact statements"), f"got {top}")
        chk("statements are situation-action-result", bool(re.search(r"(?i)situation|result", md)), "no situation/result wording")
        if name == "ask_review":
            chk("free text understood as review material", "Review material" in md[:200] or "Impact statements" in md, md[:120])
    elif name == "interview_stories":
        n = sum(1 for lv, h, _ in secs if lv == 3)
        chk("5-8 stories", 5 <= n <= 8, f"{n} stories")
        chk("stories mapped to questions", md.count("Answers:") >= n and n > 0, f"{md.count('Answers:')} mappings for {n}")
    elif name == "evidence_gaps":
        gap = next((b for _, h, b in secs_wo_ledger if h == "Evidence gaps"), "")
        n = len(blocks_of(gap))
        chk("all 8 ladder expectations listed", n == 8, f"{n} expectations")
        chk("each has status", len(re.findall(r"Status: (?:backed|thin|missing)", gap)) == n and n > 0, "status missing")
    return ok, fails


def decision_lines(md):
    out = set()
    for m in re.finditer(r"(?m)^(?:My role: \w+\.|I \w+ this\.|Decision \(\w+\):)\s*(.+)$", md):
        out.add(re.sub(r"\s*The source says this still needs a decision\.\s*$", "", m.group(1)).strip())
    return out


def project_names(name):
    p = OUT / f"{name}.csv"
    if not p.is_file():
        return set(), 0
    import csv
    rows = list(csv.reader(p.open(encoding="utf-8")))[1:]
    return {r[2] for r in rows if len(r) > 2 and r[2] not in ("(unlinked)", "(other work)")}, sum(1 for r in rows if len(r) > 2 and r[2] == "(unlinked)")


def cross_checks(names):
    """The same project names and the same decision wording in every mode, and the same items grouped into projects."""
    ok, fails = {}, []
    base_names, base_unlinked = project_names("backfill")
    base_dec = decision_lines((OUT / "backfill.md").read_text(encoding="utf-8")) if (OUT / "backfill.md").is_file() else set()
    bad_names, bad_dec, bad_unl = [], [], []
    for n in names:
        if n == "backfill":
            continue
        pn, un = project_names(n)
        if pn and pn != base_names:
            bad_names.append(f"{n}: {sorted(pn - base_names)} vs backfill {sorted(base_names - pn)}")
        if pn and un != base_unlinked:
            bad_unl.append(f"{n}: {un} unlinked rows vs backfill {base_unlinked}")
        md = (OUT / f"{n}.md").read_text(encoding="utf-8") if (OUT / f"{n}.md").is_file() else ""
        extra = decision_lines(md) - base_dec
        if extra:
            bad_dec.append(f"{n}: {sorted(extra)[:1]}")
    for label, bad in (("same project names in every mode", bad_names), ("same decision wording in every mode", bad_dec), ("same items in projects in every mode", bad_unl)):
        ok[label] = not bad
        if bad:
            fails.append(f"[all modes] {label}: " + " | ".join(bad)[:300])
    return ok, fails


def main():
    OUT.mkdir(exist_ok=True)
    check_only = "--check-only" in sys.argv
    if not check_only:
        if shutil.which("gravity") is None:
            sys.exit("gravity not found on PATH")
        moved = [f for f in ("memory.json", "store.json") if (GRAV / f).is_file()]
        if moved:
            bk = GRAV / f"backup-{time.strftime('%Y%m%d-%H%M%S')}"
            bk.mkdir(parents=True)
            for f in moved:
                shutil.move(str(GRAV / f), str(bk / f))
            print(f"memory kept (not deleted): moved to {bk}", flush=True)
    rows, all_fails = [], []
    for name, extra, ls, lc in RUNS:
        if check_only:
            wall, code = 0.0, 0
        else:
            print(f"running {name} ...", flush=True)
            wall, code = run_one(name, extra, ls)
        md = (OUT / f"{name}.md").read_text(encoding="utf-8") if (OUT / f"{name}.md").is_file() else ""
        res = json.loads((OUT / f"{name}.json").read_text()) if (OUT / f"{name}.json").is_file() else {}
        log = (OUT / f"{name}.log").read_text() if (OUT / f"{name}.log").is_file() else ""
        ok, fails = check_mode(name, md, res, wall, code, log, ls, lc)
        rows.append((name, ok, res, wall))
        all_fails += fails
        if name == "backfill" and not check_only and (not md or not res.get("ledger_rows")):
            print("\n*** BACKFILL FOUND 0 ITEMS (or no output). The sources returned nothing, so every later mode would be empty. Stopping. ***", flush=True)
            print("Last lines of out/backfill.log:\n" + "\n".join(log.strip().splitlines()[-25:]), flush=True)
            rows.append(("BACKFILL EMPTY", False, res, wall))
            all_fails.append("[backfill] 0 items fetched: check the connector errors in the log above (token expired, tool refused), then rerun")
            break
    cok, cfails = cross_checks([n for n, *_ in rows])
    rows.append(("consistency", cok, {}, 0.0))
    all_fails += cfails
    print("\nUC-10 end-to-end check\n")
    print(f"{'run':<18}{'checks':<9}{'result':<7}{'seconds':<9}calls")
    for name, ok, res, wall in rows:
        bad = sum(1 for v in ok.values() if not v)
        print(f"{name:<18}{len(ok) - bad}/{len(ok):<7}{'PASS' if not bad else 'FAIL':<7}{round(res.get('seconds', wall)):<9}{res.get('model_calls', '-')}")
    print(f"\nOVERALL: {'PASS' if not all_fails else 'FAIL (' + str(len(all_fails)) + ' failing checks)'}")
    if all_fails:
        print("\nFailing lines:")
        for f in all_fails:
            print(" - " + f[:300])
    print("\nFull outputs are in out/ (not pushed anywhere).")
    sys.exit(1 if all_fails else 0)


if __name__ == "__main__":
    main()
