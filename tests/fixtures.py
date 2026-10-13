"""Our own UC-10 fixture, built from the spec text (we do not have Gravity's samples/UC-10).
Fictional engineer Meera Joshi, 6 months, with planted truth in `expected`. Shapes follow
the GitHub / Jira / Slack REST payloads; they are ASSUMED to match Gravity's real samples
and must be re-checked against samples/UC-10 when we get them."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

USER = "meera-j"
ME = "U_MEERA"
SITE = "https://acme.atlassian.net"
START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)

PROJECTS = {
    "P1": dict(key="PAY", repo="acme/payments-api", name="UPI payout retries", words="idempotency retry payout upi gateway duplicate key backoff"),
    "P2": dict(key="PAY", repo="acme/payments-api", name="Webhook delivery queue", words="webhook queue delivery consumer dead letter replay partner"),
    "P3": dict(key="WEB", repo="acme/web", name="Checkout redesign", words="checkout cart address form layout accessibility mobile"),
    "P4": dict(key="PAY", repo="acme/payments-api", name="Settlement reconciliation job", words="settlement reconcile batch ledger mismatch timezone report"),
    "P5": dict(key="WEB", repo="acme/web", name="Design system migration", words="design system tokens button component storybook theme migrate"),
}
OTHERS = ["Arjun Rao", "Priya Nair", "Sam Lee", "Dev Patel", "Nisha Gupta", "Omar Khan"]
CUSTOMERS = ["Globex Foods", "Initech Pay"]


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(seed: int = 7, offset: int = 0) -> dict:
    r = random.Random(seed)
    ex: dict = {"projects": {}, "invisible": [], "decisions": [], "reverted_prs": [], "lockfile_pr": None, "missing_tests_prs": [],
                "role_labels": {}, "week2_ids": [], "ladder": {}, "confidential_doc": None, "customers": CUSTOMERS}
    gh = {"prs": [], "reviews": {}, "commits": []}
    jira: list[dict] = []
    slack = {"channels": [], "threads": []}
    docs: list[dict] = []
    nxt_pr = {"acme/payments-api": 800 + offset, "acme/web": 300 + offset}
    tkey = {"PAY": 400 + offset, "WEB": 100 + offset}

    def new_pr(proj, title, author, when, files, branch_key=None, body="", merged=True, state="closed", labels=()):
        repo = PROJECTS[proj]["repo"] if proj else "acme/payments-api"
        nxt_pr[repo] += 1
        n = nxt_pr[repo]
        add = sum(f["additions"] for f in files)
        dele = sum(f["deletions"] for f in files)
        pr = {"number": n, "title": title, "body": body, "html_url": f"https://github.com/{repo}/pull/{n}", "state": state,
              "user": {"login": author, "type": "User"}, "created_at": _ts(when), "merged_at": _ts(when + timedelta(hours=r.randint(4, 70))) if merged else None,
              "closed_at": _ts(when + timedelta(hours=r.randint(4, 70))), "additions": add, "deletions": dele, "changed_files": len(files),
              "head": {"ref": (branch_key or "feature").lower() + f"-{n}"}, "files": files, "labels": [{"name": l} for l in labels], "_repo": repo}
        gh["prs"].append(pr)
        return pr

    def files_small():
        return [{"filename": f"src/{r.choice(['payout','queue','cart','recon','tokens'])}{r.randint(1,9)}.py", "additions": r.randint(5, 90), "deletions": r.randint(0, 40)} for _ in range(r.randint(1, 4))]

    def new_ticket(proj, summary, desc="", assignee="Meera Joshi", reporter=None, reopened=False, done=True, links=()):
        k = PROJECTS[proj]["key"]
        tkey[k] += 1
        key = f"{k}-{tkey[k]}"
        created = START + timedelta(days=r.randint(0, 170), hours=r.randint(0, 8))
        hist = [{"created": _ts(created + timedelta(days=1)), "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]},
                {"created": _ts(created + timedelta(days=3)), "items": [{"field": "status", "fromString": "In Progress", "toString": "Done"}]}]
        if reopened:
            hist.append({"created": _ts(created + timedelta(days=5)), "items": [{"field": "status", "fromString": "Done", "toString": "In Progress"}]})
        t = {"key": key, "fields": {"summary": summary, "description": desc, "assignee": {"displayName": assignee}, "reporter": {"displayName": reporter or assignee},
                                    "created": _ts(created), "updated": _ts(created + timedelta(days=4)), "status": {"name": "Done" if done else "In Progress"},
                                    "issuetype": {"name": "Story"}, "labels": [], "comment": {"comments": []}}, "changelog": {"histories": hist}}
        jira.append(t)
        return t, created

    # ---- projects: PRs, tickets, reviews
    prs_per = {"P1": 34, "P2": 26, "P3": 28, "P4": 22, "P5": 24}  # 134 incl. special ones below
    for pid, n in prs_per.items():
        p = PROJECTS[pid]
        ex["projects"][pid] = {"name": p["name"], "items": []}
        w = p["words"].split()
        for i in range(n):
            t, created = new_ticket(pid, f"{r.choice(w).title()} {r.choice(w)} {r.choice(w)} work", f"Details about {' '.join(r.sample(w, 4))}.", reopened=(pid == "P4" and i < 2))
            when = created + timedelta(days=1)
            title = f"{t['key']}: {r.choice(w)} {r.choice(w)} {r.choice(w)}"
            use_key = r.random() < 0.8  # 20% of PRs rely on URL/topic links only
            pr = new_pr(pid, title if use_key else f"{r.choice(w)} {r.choice(w)} {r.choice(w)} {r.choice(w)} follow-up", USER, when, files_small(),
                        branch_key=t["key"] if use_key else None, body=f"Part of {p['name']}. Ticket {t['key']}\n" if use_key else f"{p['name']} change touching {' '.join(r.sample(w,3))}. See {SITE}/browse/{t['key']}")
            ex["projects"][pid]["items"] += [f"jira:{t['key']}", f"gh:pr:{pr['_repo']}#{pr['number']}"]
            ex["role_labels"][f"gh:pr:{pr['_repo']}#{pr['number']}"] = ("led" if pid in {"P1", "P4"} else "built" if pid in {"P2", "P3"} else "contributed")
    # reviews received on 11 PRs: 'missing tests' (planted improvement area), plus other feedback
    mine = [p for p in gh["prs"] if p["user"]["login"] == USER]
    for k, pr in enumerate(mine):
        revs = []
        if k < 11:
            txt = r.choice(["Please add tests for this endpoint.", "Missing tests for the new branch, can we add unit tests?", "This needs tests before merge.", "No tests here, add test coverage please."])
            revs.append({"id": 9000 + k, "user": {"login": "arjun-r"}, "state": "CHANGES_REQUESTED", "body": txt, "submitted_at": pr["created_at"]})
            ex["missing_tests_prs"].append(f"{pr['_repo']}#{pr['number']}")
        elif k % 5 == 0:
            revs.append({"id": 9500 + k, "user": {"login": "priya-n"}, "state": "APPROVED", "body": "LGTM", "submitted_at": pr["created_at"]})
        revs.append({"id": 9900 + k, "user": {"login": "dependabot[bot]", "type": "Bot"}, "state": "COMMENTED", "body": "bump", "submitted_at": pr["created_at"]})
        gh["reviews"][f"{pr['_repo']}#{pr['number']}"] = revs
    # 6 reverted: revert PRs
    for pr in r.sample([p for p in mine if p["number"] % 2 == 0][:20], 6):
        rep = new_pr(None, f'Revert "{pr["title"]}"', USER, datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")) + timedelta(days=2), files_small(), body=f"This reverts pull request #{pr['number']}.")
        rep["_repo"] = pr["_repo"]; rep["html_url"] = f"https://github.com/{pr['_repo']}/pull/{rep['number']}"
        ex["reverted_prs"].append(f"gh:pr:{pr['_repo']}#{pr['number']}")
    # lockfile PR, 4000 lines
    lp = new_pr("P5", "WEB-199: bump deps", USER, START + timedelta(days=60), [{"filename": "package-lock.json", "additions": 3900, "deletions": 100}, {"filename": "package.json", "additions": 4, "deletions": 4}], branch_key="WEB-199")
    ex["lockfile_pr"] = f"gh:pr:acme/web#{lp['number']}"
    # reviews Meera gave on others' PRs: 310
    for i in range(310):
        pid = r.choice(list(PROJECTS))
        repo = PROJECTS[pid]["repo"]
        nxt_pr[repo] += 1
        n = nxt_pr[repo]
        when = START + timedelta(days=r.randint(0, 170), hours=r.randint(0, 8))
        author = r.choice(OTHERS).lower().replace(" ", "-")
        pr = {"number": n, "title": f"{PROJECTS[pid]['key']}-{r.randint(1,400)}: {r.choice(PROJECTS[pid]['words'].split())} change", "body": "", "html_url": f"https://github.com/{repo}/pull/{n}", "state": "closed",
              "user": {"login": author, "type": "User"}, "created_at": _ts(when), "merged_at": _ts(when + timedelta(hours=20)), "closed_at": _ts(when + timedelta(hours=20)),
              "additions": 30, "deletions": 5, "head": {"ref": "feat-x"}, "files": [], "labels": [], "_repo": repo}
        gh["prs"].append(pr)
        gh["reviews"][f"{repo}#{n}"] = [{"id": 20000 + i, "user": {"login": USER}, "state": "APPROVED", "body": r.choice(["Looks good", "Nit: rename this", "Approved, thanks"]), "submitted_at": _ts(when + timedelta(hours=3))}]
    # ---- ops/research tickets with no PR
    for i in range(83):
        new_ticket("P1", f"Ops task {i}: rotate credentials", "Routine ops, no code change")
    # ---- docs: 35, 3 are decision docs, 1 confidential
    for i in range(28):
        pid = r.choice(list(PROJECTS))
        docs.append({"id": f"d{i}", "title": f"{PROJECTS[pid]['name']} notes {i}", "body": f"Notes on {PROJECTS[pid]['words']}. " + (f"Ticket {PROJECTS[pid]['key']}-{r.randint(401,430)}." if i % 2 else ""),
                     "author": "Meera Joshi" if i % 3 == 0 else r.choice(OTHERS), "editors": ["Meera Joshi"] if i % 3 else [], "modified": _ts(START + timedelta(days=r.randint(0, 170))), "url": f"https://acme.atlassian.net/wiki/spaces/ENG/pages/{1000+i}", "labels": [],
                     "_mine": True, "_attribution": "CQL creator/contributor = currentUser() filter"})
    decisions = [("P2", "ADR: move webhooks to a queue", "We decided to deliver webhooks through a queue instead of inline calls. Options: inline retry, cron replay, queue. Chosen: queue. Proposed by Meera Joshi; platform team consulted.", "led"),
                 ("P1", "ADR: idempotency keys on payouts", "Decision: use client generated idempotency keys stored for 24h. Alternatives rejected: dedupe table. Author Meera Joshi.", "led"),
                 ("P5", "ADR: tokens before components", "Decision: migrate design tokens first, components after. Meera contributed the comparison; decided by Sam Lee.", "contributed")]
    for k, (pid, title, body, role) in enumerate(decisions):
        d = {"id": f"adr{k}", "title": title, "body": body + f" {PROJECTS[pid]['words']}", "author": "Meera Joshi" if role == "led" else "Sam Lee", "editors": ["Meera Joshi"], "modified": _ts(START + timedelta(days=40 + k * 30)),
             "_mine": True, "_attribution": "CQL creator/contributor = currentUser() filter",
             "url": f"https://acme.atlassian.net/wiki/spaces/ENG/pages/{2000+k}", "labels": ["adr"]}
        docs.append(d)
        ex["decisions"].append({"id": f"doc:confluence:adr{k}", "role": role})
    conf = {"id": "conf1", "title": "Q3 Payments pricing plan (confidential)", "body": "CONFIDENTIAL. Do not share. Pricing for Globex Foods renewal.", "author": "Meera Joshi", "editors": [], "modified": _ts(START + timedelta(days=100)),
            "_mine": True, "_attribution": "CQL creator/contributor = currentUser() filter",
            "url": "https://acme.atlassian.net/wiki/spaces/FIN/pages/3000", "labels": []}
    docs.append(conf)
    ex["confidential_doc"] = "doc:confluence:conf1"
    # ---- slack: 12 channels (+1 DM), ~4000 messages
    chans = ["payments-eng", "incidents", "web-platform", "random", "design", "oncall", "standup", "releases", "help-eng", "data", "security", "ops"]
    slack["channels"] = [{"id": f"C{i:03d}", "name": c, "is_im": False} for i, c in enumerate(chans)] + [{"id": "D001", "name": "dm-arjun", "is_im": True}]
    cid = {c["name"]: c["id"] for c in slack["channels"]}
    tsn = [1_773_000_000.0]
    msgs_total = 0

    def thread(channel, texts, by, when, bots=False):
        nonlocal msgs_total
        base = when.timestamp()
        ms = []
        for j, (u, t) in enumerate(zip(by, texts)):
            ms.append({"ts": f"{base + j*60:.6f}", "user": u, "user_name": ("Meera Joshi" if u == ME else u), "text": t})
        if bots:
            ms.append({"ts": f"{base + 999:.6f}", "user": "UBOT", "bot_id": "B1", "text": "deploy finished"})
        slack["threads"].append({"channel": channel, "channel_id": cid[channel], "messages": ms})
        msgs_total += len(ms)
        return f"slack:{cid[channel]}:{ms[0]['ts']}"

    inv = [("incidents", "Settlement totals don't match for 18 Sep batch, any idea?", "Traced it: the batch job used local time, UTC offset shifts the cutoff by 5.5h. Pushing a fix to the job config.", "Arjun Rao"),
           ("payments-eng", "How do I replay a failed payout manually?", "Use the replay script with the payout id, then check the ledger. Here is the exact command.", "Dev Patel"),
           ("help-eng", "New joiner: how does idempotency work here?", "Walked through the key lifecycle and where it is stored, onboarded the new joiner for an hour.", "Nisha Gupta"),
           ("web-platform", "Our build is stuck on the tokens package", "Cleared the stale cache and pinned the version, build is green again.", "Sam Lee"),
           ("oncall", "Pager fired for webhook lag at 3am", "Took the page, found the consumer stuck on a poison message, moved it to the dead letter queue.", "Omar Khan"),
           ("data", "Why is the settlement report empty for UPI?", "The filter excluded the new status. Explained the status mapping and patched the query.", "Priya Nair"),
           ("security", "Is it okay to log the partner callback url?", "Advised against it, shared the redaction helper, reviewed the change together.", "Dev Patel"),
           ("design", "Contrast failing on the checkout button", "Showed the token to use and why, mentored on the accessibility check.", "Nisha Gupta"),
           ("ops", "Cron job double running on deploy", "Added a lock and wrote the runbook entry, no ticket was raised for it.", "Omar Khan")]
    for k, (ch, q, a, who) in enumerate(inv):
        when = START + timedelta(days=15 + k * 17, hours=2)
        sid = thread(ch, [q, a], [who, ME], when)
        ex["invisible"].append(sid)
    # project-channel threads (carry ticket key or PR url sometimes)
    for pid in PROJECTS:
        p = PROJECTS[pid]
        w = p["words"].split()
        for k in range(24):
            when = START + timedelta(days=r.randint(0, 170), hours=r.randint(0, 8))
            ch = {"P1": "payments-eng", "P2": "payments-eng", "P3": "web-platform", "P4": "incidents", "P5": "design"}[pid]
            tid = r.choice([x for x in ex["projects"][pid]["items"] if x.startswith("jira:")]).split(":")[1]
            txt = f"{r.choice(w)} {r.choice(w)} update, {r.choice(w)} status " + (f"see {tid}" if k % 2 == 0 else "")
            sid = thread(ch, [txt, f"ok, {r.choice(w)} done {r.choice(w)}", "thanks"], [ME, r.choice(OTHERS), ME], when)
            ex["projects"][pid]["items"].append(sid)
    # noise: threads Meera is not part of, bot noise, DM
    for k in range(250):
        ch = r.choice(chans)
        when = START + timedelta(days=r.randint(0, 175))
        thread(ch, [f"random chatter {r.randint(1,999)} {r.choice(['lunch','offsite','wifi'])}", "agree"], [r.choice(OTHERS), r.choice(OTHERS)], when, bots=(k % 3 == 0))
    slack["threads"].append({"channel": "dm-arjun", "channel_id": "D001", "messages": [{"ts": f"{(START + timedelta(days=30)).timestamp():.6f}", "user": ME, "user_name": "Meera Joshi", "text": "private note: salary talk"}]})
    # filler messages to reach ~4000
    while msgs_total < 4000:
        ch = r.choice(chans)
        thread(ch, [f"msg {r.randint(1,99999)}"], [r.choice(OTHERS)], START + timedelta(days=r.randint(0, 175)))
    ex["slack_messages"] = msgs_total

    # ladder: 8 next-level expectations
    ex["ladder_text"] = """Software Engineer II
- Delivers features independently
- Reviews teammates' code
SE III (next level)
- Owns a system end to end and its reliability
- Designs and documents technical solutions
- Reviews code and gives useful feedback
- Mentors engineers and unblocks teammates
- Leads a cross-team technical decision
- Improves quality through testing and automation
- Communicates status and risks to stakeholders
- Drives incident response and debugging
"""
    ex["ladder"] = {"missing": "Leads a cross-team technical decision", "thin": "Mentors engineers and unblocks teammates"}

    # ---- week 2: 14 new items
    w2 = []
    base = START + timedelta(days=182)
    for k in range(4):
        pr = new_pr("P2", f"PAY-{500+k}: queue worker tweak {k}", USER, base + timedelta(days=k), files_small(), branch_key=f"PAY-{500+k}")
        w2.append(f"gh:pr:{pr['_repo']}#{pr['number']}")
    for k in range(3):
        t, c = new_ticket("P2", f"Week two ticket {k}")
        t["fields"]["created"] = _ts(base + timedelta(days=k)); t["fields"]["updated"] = _ts(base + timedelta(days=k + 1)); t["fields"].pop("resolutiondate", None)
        t["changelog"]["histories"] = []
        w2.append(f"jira:{t['key']}")
    for k in range(5):
        w2.append(thread("payments-eng", [f"webhook queue follow up {k}", "ack"], [ME, "Arjun Rao"], base + timedelta(days=k, hours=3)))
    for k in range(2):
        docs.append({"id": f"w2d{k}", "title": f"Webhook queue runbook {k}", "body": "queue runbook", "author": "Meera Joshi", "editors": [], "modified": _ts(base + timedelta(days=k)), "url": f"https://acme.atlassian.net/wiki/spaces/ENG/pages/{4000+k}", "labels": [],
                     "_mine": True, "_attribution": "CQL creator/contributor = currentUser() filter"})
        w2.append(f"doc:confluence:w2d{k}")
    ex["week2_ids"] = w2
    ex["week2_start"] = _ts(base - timedelta(hours=1))
    return {"github": gh, "jira": jira, "slack": slack, "docs": docs, "expected": ex}

