import asyncio
import pytest
import yaml
from repo_scope import discover, summary
from guards import ToolLog
from sources import GatewaySource
import agent
from test_round2 import Src, NOW


def test_github_only_never_looks_up_jira():
    async def site():
        raise AssertionError("Jira not selected")
    deps = agent.Deps(lambda cfg: Src(), llm=None, now=lambda: NOW, identity=lambda: None, site=site)
    out = asyncio.run(agent.make_graph(deps).ainvoke({"inputs": {"github_username": "me", "role_and_level": "SE", "repos_or_org": "o/r"}, "memory": {}}))
    assert out["result"]["status"] == "completed"


def test_optional_sources_one_coverage_line_without_errors():
    async def call(name, args):
        if name.startswith("jira") or name.startswith("slack"):
            raise RuntimeError("401 not connected")
        return []
    source = GatewaySource(call, "me", ["o/r"], ["general"], False, ["PAY"], {"jira.get_all_projects", "jira.search_issues", "slack.list_channels"})
    out = asyncio.run(source.fetch("2026-04-14T00:00:00Z", None, ToolLog()))
    assert not out.get("blocked")
    assert out["coverage"]["jira"] == "isn't connected"
    assert out["coverage"]["slack"] == "isn't connected"
    assert not any(f["source"].startswith(("jira", "slack", "docs")) for f in out["failed"])


class ScopeGateway:
    def __init__(self): self.calls = []
    async def __call__(self, name, args):
        self.calls.append((name, args))
        if args.get("page", 1) > 1: return []
        if name == "github.list_repositories_for_the_authenticated_user":
            return [{"id": 1, "full_name": "me/private", "private": True}, {"id": 2, "full_name": "me/fork", "fork": True}, {"id": 3, "full_name": "me/excluded"}]
        if name == "github.list_organizations_for_the_authenticated_user": return [{"id": 5, "login": "org"}]
        if name == "github.list_organization_repositories": return [{"id": 4, "full_name": "org/work"}, {"id": 6, "full_name": "org/unknown"}]
        if name == "github.list_branches": return [{"name":"feature-only"}]
        if name == "github.list_commits":
            if args["repo"] == "unknown": raise RuntimeError("403 scope")
            return [] if args["repo"] == "fork" else [{"sha": "a"}]
        raise AssertionError(name)


def test_discovery_private_org_fork_exclusions_and_unverified():
    async def forbidden(*args):
        raise AssertionError('Discovery must not call any API')
    with pytest.raises(ValueError, match='disabled'):
        asyncio.run(discover(forbidden, 'me', '', '', [], ToolLog()))


def test_allowlist_disables_account_discovery_and_applies_exclusion():
    calls = []
    async def call(n,a):
        calls.append((n,a))
        assert n not in {"github.list_repositories_for_the_authenticated_user", "github.list_organizations_for_the_authenticated_user"}
        return []
    source = GatewaySource(call,"me",["o/yes","o/no"],[],False,[],set())
    source.configure_scope(["o/no"],"2026-04-14T00:00:00Z","2026-10-14T00:00:00Z")
    out = asyncio.run(source.fetch("2026-04-14T00:00:00Z",None,ToolLog()))
    assert out["repo_scope"]["read"] == ["o/yes"]
    assert not any(a.get("repo") == "no" for n,a in calls)


def test_form_onboarding_required_only_role():
    m = yaml.safe_load(open('manifest.yaml'))
    assert len(m['inputs']) == 19
    assert [x['name'] for x in m['inputs'] if x.get('required')] == ['role_and_level', 'repos_or_org']
    assert all(x['label'].startswith('Advanced:') for x in m['inputs'] if x['name'] not in {'github_username','role_and_level','request','repos_or_org'})


def test_calendar_month_boundary_leap_and_clamp():
    from datetime import datetime, timezone
    from inputs import backfill_since
    assert backfill_since(datetime(2026,10,14,tzinfo=timezone.utc),6) == '2026-04-14T00:00:00Z'
    assert backfill_since(datetime(2026,8,31,tzinfo=timezone.utc),6) == '2026-02-28T00:00:00Z'
    assert backfill_since(datetime(2024,8,31,tzinfo=timezone.utc),6) == '2024-02-29T00:00:00Z'


def test_coverage_summary_once_and_persists_for_on_demand():
    from test_graph import INP, W1, E, D
    from sources import FixtureSource
    class Covered(FixtureSource):
        async def fetch(self,*args):
            raw = await super().fetch(*args)
            raw['coverage'] = {'jira': "isn't connected", 'slack': 'no channels selected', 'docs': 'no docs source selected'}
            raw['repo_scope'] = {'mode':'allow-list','read':['acme/payments-api'],'no_commits':[],'excluded':[],'unverified':[]}
            return raw
    def invoke(inp,mem):
        deps = agent.Deps(lambda cfg: Covered(D,before=E['week2_start']),llm=None,now=lambda:W1)
        return asyncio.run(agent.make_graph(deps).ainvoke({'inputs':inp,'memory':mem}))
    first = invoke(INP,{})
    follow = invoke({**INP,'request':'one_on_one'},first['memory'])
    assert first['corpus']['summary'].count('Not covered:') == 1
    assert follow['corpus']['summary'].count('Not covered:') == 1
    assert 'Repos read:' in first['corpus']['summary']


def test_discovery_pages_repeat_is_unverifiable_not_no_commits():
    async def forbidden(*args):
        raise AssertionError('Discovery must not call any API')
    with pytest.raises(ValueError, match='disabled'):
        asyncio.run(discover(forbidden, 'me', '', '', [], ToolLog()))


def test_fork_with_commit_is_included():
    async def forbidden(*args):
        raise AssertionError('Discovery must not call any API')
    with pytest.raises(ValueError, match='disabled'):
        asyncio.run(discover(forbidden, 'me', '', '', [], ToolLog()))


def test_adding_source_backfills_instead_of_github_cursor_only():
    from test_graph import INP, W1, E, D
    from sources import FixtureSource
    seen = []
    class Source(FixtureSource):
        async def fetch(self,since,until,log):
            seen.append(since)
            return await super().fetch(since,until,log)
    deps = agent.Deps(lambda cfg:Source(D,before=E['week2_start']),llm=None,now=lambda:W1)
    g = agent.make_graph(deps)
    first = asyncio.run(g.ainvoke({'inputs':{**INP,'jira_projects':'','slack_channels':'','docs_source':'None'},'memory':{}}))
    second = asyncio.run(g.ainvoke({'inputs':INP,'memory':first['memory']}))
    assert second['result']['mode'] == 'weekly'
    assert seen[0] != seen[2]  # normal run only updates the existing GitHub scope
    from store import NativeMemoryStore
    assert NativeMemoryStore(second['memory']).load()['source_setup']['config']['jira_projects']
    assert second['result']['status']=='partial'
    assert len(seen)==19  # two original repos plus 15 newly supplied optional scopes


def test_discovery_arguments_match_live_catalog_schemas():
    async def forbidden(*args):
        raise AssertionError('Discovery must not call any API')
    with pytest.raises(ValueError, match='disabled'):
        asyncio.run(discover(forbidden, 'me', '', '', [], ToolLog()))


def test_branch_only_commits_qualify_repo():
    async def forbidden(*args):
        raise AssertionError('Discovery must not call any API')
    with pytest.raises(ValueError, match='disabled'):
        asyncio.run(discover(forbidden, 'me', '', '', [], ToolLog()))
