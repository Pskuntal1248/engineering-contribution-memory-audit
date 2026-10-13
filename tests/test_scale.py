"""3x the spec's backfill volume with a counting fake model: how many calls, how big each payload, how long it takes.
The model is a FAKE that answers deterministically, so this proves call counts, payload caps and run time of OUR code only,
not model quality or real model latency."""
import asyncio
import json
import time
from datetime import timedelta

import fixtures

import agent
from analysis import DecisionVerdict, DecisionVerdicts, ProjectCard, ProjectCards, ThreadVerdict, ThreadVerdicts
from sources import FixtureSource

ME = fixtures.USER


def triple() -> dict:
    parts = [fixtures.build(seed=7 + k, offset=1000 * k) for k in range(3)]
    raw = {"github": {"prs": [], "reviews": {}, "commits": []}, "jira": [], "slack": {"channels": parts[0]["slack"]["channels"], "threads": []}, "docs": []}
    for k, d in enumerate(parts):
        raw["github"]["prs"] += d["github"]["prs"]
        raw["github"]["reviews"].update(d["github"]["reviews"])
        raw["jira"] += d["jira"]
        for t in d["slack"]["threads"]:
            for m in t["messages"]:
                m["ts"] = f"{float(m['ts']) + k * 3:.6f}"
            raw["slack"]["threads"].append(t)
        for doc in d["docs"]:
            doc["id"] = f"{doc['id']}-{k}"
            raw["docs"].append(doc)
    return raw


class CountingLLM:
    def __init__(self):
        self.calls, self.sizes = 0, []

    async def structured(self, schema, system, payload, budget=None):
        if budget is not None:
            budget.spend()
        self.calls += 1
        self.sizes.append(len(json.dumps(payload, default=str)))
        if schema is ThreadVerdicts:
            return ThreadVerdicts(verdicts=[ThreadVerdict(thread_id=t["thread_id"], label="problem_solved", summary="fixed it") for t in payload["threads"]])
        if schema is DecisionVerdicts:
            return DecisionVerdicts(verdicts=[DecisionVerdict(item_id=i["item_id"], is_decision=True, summary="decided") for i in payload["items"]])
        return ProjectCards(projects=[ProjectCard(project_id=p["project_id"], name="P " + p["project_id"], goal="g", outcome="o", role="built",
                                                  evidence=[e["id"] for e in p["evidence"]]) for p in payload["projects"]])


def test_three_times_the_spec_volume_stays_inside_the_caps():
    raw = triple()
    n_msgs = sum(len(t["messages"]) for t in raw["slack"]["threads"])
    n_prs, n_rev, n_tix = len(raw["github"]["prs"]), sum(len(v) for v in raw["github"]["reviews"].values()), len(raw["jira"])
    llm = CountingLLM()
    now = fixtures.START + timedelta(days=176)
    deps = agent.Deps(lambda cfg: FixtureSource(raw), llm=llm, now=lambda: now)
    inp = {"github_username": ME, "repos_or_org": "acme/payments-api, acme/web", "jira_site": fixtures.SITE, "jira_projects": "PAY, WEB",
           "slack_channels": "#payments-eng, #incidents, #web-platform, #random, #design, #oncall, #standup, #releases, #help-eng, #data, #security, #ops",
           "include_dms": "No", "display_name": "Meera Joshi", "slack_user_ids": fixtures.ME, "role_and_level": "Software Engineer II", "backfill_months": 6}
    t0 = time.monotonic()
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": inp, "memory": {}}))
    secs = time.monotonic() - t0
    mine = sum(1 for p in raw["github"]["prs"] if p["user"]["login"] == ME)
    print(f"\nSCALE: {n_prs} PRs ({mine} mine), {n_rev} reviews, {n_tix} tickets, {n_msgs} Slack messages in {len(raw['slack']['threads'])} threads, {len(raw['docs'])} docs")
    print(f"SCALE: items kept={out['result']['ledger_rows']} model_calls={llm.calls} biggest_payload_chars={max(llm.sizes)} total_payload_chars={sum(llm.sizes)} seconds={secs:.1f}")
    print(f"SCALE: projects={out['result']['projects']} invisible={out['result']['invisible_work']} decisions={out['result']['decisions']} notes={out['result']['notes'][:4]}")
    assert out["result"]["mode"] == "backfill"
    assert llm.calls <= 150 and out["result"]["model_calls"] == llm.calls
    assert max(llm.sizes) < 60_000  # every call carries a bounded slice, never the whole history
    assert secs < 120
    assert out["result"]["dm_reads"] == 0
