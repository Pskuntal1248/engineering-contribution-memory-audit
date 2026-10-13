import asyncio

import fixtures
import pytest

import agent
from sources import FixtureSource
from test_graph import D, E, INP, W1


def run(inputs, identity, site=None):
    deps = agent.Deps(lambda cfg: FixtureSource(D, before=E["week2_start"]), llm=None, now=lambda: W1, identity=identity, site=site)
    return asyncio.run(agent.make_graph(deps).ainvoke({"inputs": inputs, "memory": {}}))


def test_blank_username_is_read_from_connected_account():
    seen = []

    async def who():
        seen.append(1)
        return {"login": fixtures.USER, "name": "Meera Joshi"}
    out = run({**INP, "github_username": "", "display_name": ""}, who)
    assert seen and out["result"]["mode"] == "backfill"
    assert any("taken from the connected GitHub account" in str(s) for s in [out["corpus"]])


def test_typed_username_wins_and_account_not_called():
    async def who():
        raise AssertionError("must not be called")
    out = run(INP, who)
    assert out["result"]["mode"] == "backfill"


def test_blank_and_unresolvable_fails_clearly():
    async def who():
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError, match="Could not read your GitHub username"):
        run({**INP, "github_username": ""}, who)
    with pytest.raises(RuntimeError, match="type your username"):
        run({**INP, "github_username": ""}, None)


def test_blank_jira_site_comes_from_account_and_failure_is_clear():
    async def site():
        return "https://acme.atlassian.net/"
    out = run({**INP, "jira_site": ""}, None, site=site)
    assert out["result"]["mode"] == "backfill"

    async def nosite():
        return ""
    with pytest.raises(RuntimeError, match="Could not read the Jira site"):
        run({**INP, "jira_site": ""}, None, site=nosite)

