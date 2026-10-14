import asyncio
from datetime import datetime, timezone
import copy
import pytest
import agent
import eprofile
from models import Item
from sources import GatewaySource
from guards import ToolLog
from inputs import validate_inputs
from store import NativeMemoryStore

NOW=datetime(2026,10,14,tzinfo=timezone.utc)

def item(repo, n):
 return Item(id=f'gh:commit:{repo}@{n}',source='github',type='commit',date='2026-10-01T10:00:00Z',title=f'Implement handler {n}',author='me',url=f'https://github.com/{repo}/commit/{n}',meta={'mine':True,'repo':repo})

def memory():
 its=[item('o/yes',1),item('o/yes',2),item('o/no',3),item('o/no',4)]
 p={'items':[eprofile.compact(i,'bad-old') for i in its],'seen':['keep'],'cursors':{'github':'2026-10-13'},'last_run':'2026-10-13','backfill_done':True,'scope_sync':{'github:o/yes':{'complete':True,'cursor':'2026-10-13T00:00:00Z'}},'repo_scope':{'mode':'discovery','read':['o/yes','o/no']}}
 return NativeMemoryStore({}).dump(p)

@pytest.mark.parametrize('text',['','org','org, o/r','https://github.com/o/r'])
def test_missing_or_owner_only_fails_before_calls(text):
 assert validate_inputs({'repos_or_org':text})
 async def forbidden(*a):raise AssertionError('No calls')
 src=GatewaySource(forbidden,'me',[text] if text else [],[],False,[],set())
 out=asyncio.run(src.fetch('2026-04-14',None,ToolLog()))
 assert out['blocked']

@pytest.mark.parametrize('mode',list(agent.ON_DEMAND))
def test_selected_saved_only_outputs_hide_broad_evidence_without_state_change(mode):
 mem=memory();original=copy.deepcopy(mem)
 def forbidden(*a):raise AssertionError('No source')
 graph=agent.make_graph(agent.Deps(forbidden,llm=None,now=lambda:NOW))
 out=asyncio.run(graph.ainvoke({'inputs':{'github_username':'me','repos_or_org':'o/yes','role_and_level':'Engineer','request':mode},'memory':mem}))
 assert all(i.meta['repo']=='o/yes' for i in out['ctx']['items'])
 assert 'o/no' not in str(out['corpus'])
 assert mem==original
 saved=NativeMemoryStore(out['memory']).load();old=NativeMemoryStore(original).load()
 assert saved['items']==old['items'] and saved['seen']==old['seen'] and saved['cursors']==old['cursors']
 assert saved['source_setup']['config']['repos_or_org']=='o/yes' 
 assert any('2 previously saved items' in n for n in out['result']['notes'])


def test_multi_selection_dedup_no_account_scan():
 calls=[]
 async def call(n,a):
  calls.append((n,a));assert not any(x in n for x in ['repositories','organizations']);return []
 src=GatewaySource(call,'me',['O/One','o/two','o/one'],[],False,[],set())
 src.configure_scope([], '2026-04-14','2026-10-14')
 out=asyncio.run(src.fetch('2026-04-14',None,ToolLog()))
 assert out['repo_scope']['read']==['o/one','o/two']


def test_scope_change_retains_history_but_uses_selected_backfill_window():
 calls=[]
 class Source:
  async def fetch(self,since,until,log):
   calls.append(since);return {'github':{'prs':[],'reviews':{},'commits':[]},'jira':[],'slack':{'channels':[],'threads':[]},'docs':[],'failed':[],'capped':[]}
  def configure_scope(self,*a):pass
 graph=agent.make_graph(agent.Deps(lambda cfg:Source(),llm=None,now=lambda:NOW))
 out=asyncio.run(graph.ainvoke({'inputs':{'github_username':'me','repos_or_org':'o/yes','role_and_level':'Engineer','request':'weekly_update'},'memory':memory()}))
 saved=NativeMemoryStore(out['memory']).load()
 assert out['result']['mode']=='weekly' and calls[0].startswith('2026-10-12')
 assert len(saved['items'])==4 and 'keep' in saved['seen']
 assert saved['cursors']['github']=='2026-10-13'
 assert saved['selected_repos']==['o/yes'] and saved['selected_github_cursor'].startswith('2026-10-01')
 assert all(i.meta['repo']=='o/yes' for i in out['ctx']['items'])
