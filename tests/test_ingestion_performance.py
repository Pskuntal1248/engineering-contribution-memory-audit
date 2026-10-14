"""Offline call-count/coverage checks, not live latency benchmarks."""
import asyncio
from guards import ToolLog
from sources import GatewaySource

HAS = {'github.list_commits','github.list_branches','github.get_commit','github.search_issues'}
START, END = '2026-04-14T00:00:00Z','2026-10-14T00:00:00Z'

def row(n):
    return {'sha':f'{n:040x}','files':[{'filename':'x.py'}], 'commit':{'message':f'Change {n}','author':{'date':'2026-09-01T00:00:00Z'}}}

class Gateway:
    def __init__(self, count=1000, repeated_heads=True, fail_page=None):
        self.count,self.repeated_heads,self.fail_page=count,repeated_heads,fail_page
        self.calls=[]
    async def __call__(self,name,args):
        self.calls.append((name,dict(args)))
        if name=='github.list_repositories_for_the_authenticated_user':
            return [] if args['page']>1 else [{'id':1,'full_name':'me/r','default_branch':'main'}]
        if name=='github.list_organizations_for_the_authenticated_user': return []
        if name=='github.list_branches':
            if args['page']>1:return []
            if self.repeated_heads:return [{'name':f'b{i}','commit':{'sha':f'{0:040x}'}} for i in range(20)]+[{'name':'main','commit':{'sha':f'{0:040x}'}}]
            return [{'name':'main'},{'name':'feature'}]
        if name=='github.list_commits':
            assert args['author']=='me' and args['since']==START and args['until']==END
            if self.fail_page==args['page']:raise RuntimeError('503 unavailable')
            data=[row(i) for i in range(self.count)]
            if args.get('sha')=='feature':data.append(row(self.count))
            lo=(args['page']-1)*args['per_page'];return data[lo:lo+args['per_page']]
        if name=='github.search_issues':return {'items':[]}
        raise RuntimeError(name)

def fetch(gw, discovery=True):
    has=HAS|{'github.list_repositories_for_the_authenticated_user','github.list_organizations_for_the_authenticated_user'}
    src=GatewaySource(gw,'me',[] if discovery else ['me/r'],[],False,[],has)
    src.configure_scope([],START,END)
    return asyncio.run(src.fetch(START,END,ToolLog()))

def test_1000_commits_not_truncated_and_default_qualification_page_reused():
    gw=Gateway();out=fetch(gw)
    assert len(out['github']['commits'])==1000 and not out.get('github_incomplete')
    calls=[a for n,a in gw.calls if n=='github.list_commits']
    assert len(calls)==11 #10full pages+empty, not20branches*11, qualification reused
    assert all('sha' not in a for a in calls)
    assert not any('first 300' in c for c in out['capped'])

def test_distinct_feature_history_still_read_and_sha_deduped():
    gw=Gateway(count=220,repeated_heads=False);out=fetch(gw)
    assert len(out['github']['commits'])==221
    assert any(a.get('sha')=='feature' for n,a in gw.calls if n=='github.list_commits')

def test_failure_retains_page_evidence_and_marks_incomplete():
    gw=Gateway(fail_page=3);out=fetch(gw)
    assert len(out['github']['commits'])==200 and out['github_incomplete']
    assert any('503' in f['error'] for f in out['failed'])

def test_same_heads_without_default_metadata_scan_once():
    gw=Gateway();out=fetch(gw,discovery=False)
    assert len(out['github']['commits'])==1000
    assert len([1 for n,a in gw.calls if n=='github.list_commits'])==11

def test_repeat_page_fails_instead_of_hiding_partial_history():
    class Repeated(Gateway):
        async def __call__(self,n,a):
            return await super().__call__(n,{**a,'page':1} if n=='github.list_commits' else a)
    out=fetch(Repeated())
    assert out['github_incomplete'] and len(out['github']['commits'])==100

def test_partial_graph_does_not_mark_backfill_done_or_advance_cursor():
    import agent
    from datetime import datetime,timezone
    gw=Gateway(fail_page=3)
    deps=agent.Deps(lambda cfg:GatewaySource(gw,'me',cfg.repos,[],False,[],HAS),llm=None,now=lambda:datetime(2026,10,14,tzinfo=timezone.utc))
    out=asyncio.run(agent.make_graph(deps).ainvoke({'inputs':{'github_username':'me','repos_or_org':'me/r','role_and_level':'Engineer'},'memory':{}}))
    from store import NativeMemoryStore
    saved=NativeMemoryStore(out['memory']).load()
    assert not saved['backfill_done'] and not saved['cursors'].get('github')


def test_exclusions_before_reads_and_allowlist_no_account_discovery():
    gw=Gateway()
    src=GatewaySource(gw,'me',['me/r','me/excluded'],[],False,[],HAS)
    src.configure_scope(['me/excluded'],START,END)
    out=asyncio.run(src.fetch(START,END,ToolLog()))
    assert len(out['github']['commits'])==1000
    assert not any(a.get('repo')=='excluded' for _,a in gw.calls)
    assert not any(n.startswith('github.list_repositories') for n,_ in gw.calls)


def test_discovery_failure_does_not_allow_cursor_advance():
    class Failure(Gateway):
        async def __call__(self,n,a):
            if n=='github.list_commits':raise RuntimeError('403 forbidden')
            return await super().__call__(n,a)
    out=fetch(Failure())
    assert out['github_incomplete'] and out['repo_scope']['unverified']==['me/r']
