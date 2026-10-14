import asyncio
import copy
from datetime import datetime, timezone
import pytest
import agent
import source_setup as setup
from store import NativeMemoryStore

NOW=datetime(2026,10,14,tzinfo=timezone.utc)
BASE={'github_username':'me','repos_or_org':'o/one','role_and_level':'Engineer','request':'weekly_update'}

class Source:
 def __init__(self,cfg,calls,fail):self.cfg,self.calls,self.fail=cfg,calls,fail
 def configure_scope(self,*a):pass
 async def fetch(self,since,until,log):
  spec=(tuple(self.cfg.repos),tuple(self.cfg.jira_projects),tuple(self.cfg.slack_channels),tuple(self.cfg.docs_sources))
  self.calls.append((spec,since))
  return {'github':{'prs':[],'reviews':{},'commits':[]},'jira':[],'slack':{'channels':[],'threads':[]},'docs':[],
          'failed':[{'source':'slack','error':'unavailable'}] if self.fail and self.cfg.slack_channels else [],'capped':[],
          'coverage':{'slack':'covered','jira':'covered','docs':'covered'}}

def graph(calls,fail=False):return agent.make_graph(agent.Deps(lambda cfg:Source(cfg,calls,fail),llm=None,now=lambda:NOW))
def invoke(g,inp,mem=None):return asyncio.run(g.ainvoke({'inputs':inp,'memory':mem or {}}))
def run(g,inp,mem=None):return invoke(g,inp,mem)
def saved(out):return NativeMemoryStore(out['memory']).load()



def test_new_source_enters_requested_report_without_actions_and_no_github_refetch():
 calls=[];g=graph(calls);first=invoke(g,BASE);calls.clear()
 out=invoke(g,{**BASE,'request':'one_on_one','slack_channels':'general'},first['memory'])
 assert len(calls)==1 and calls[0][0][2]==('general',) and not calls[0][0][0]
 assert saved(out)['source_setup']['config']['slack_channels']=='general'
 assert out['result']['mode']=='one_on_one' and out['result']['status']=='partial'
 cursor=saved(out)['scope_sync']['slack:general:No']['history_cursor'];calls.clear()
 nxt=invoke(graph(calls),{**BASE,'request':'interview_stories'},out['memory'])
 assert len(calls)==1 and calls[0][1]==cursor and not calls[0][0][0]
 assert saved(nxt)['scope_sync']['slack:general:No']['history_cursor']>cursor
 assert out['result']['model_calls']<=10 and nxt['result']['model_calls']<=10


def test_failure_does_not_advance_history_and_retry_does_not_read_github():
 calls=[];first=invoke(graph(calls),BASE);calls.clear()
 bad=invoke(graph(calls,True),{**BASE,'request':'one_on_one','slack_channels':'general'},first['memory'])
 assert saved(bad)['scope_sync']['slack:general:No']['history_cursor']==''
 calls.clear();good=invoke(graph(calls),{**BASE,'request':'one_on_one'},bad['memory'])
 assert len(calls)==1 and saved(good)['scope_sync']['slack:general:No']['history_cursor']


def test_removal_is_explicit_blank_is_keep_and_stale_fields_do_not_restore():
 calls=[];first=invoke(graph(calls),{**BASE,'slack_channels':'general'})
 blank=invoke(graph(calls),{**BASE,'request':'one_on_one'},first['memory'])
 assert saved(blank)['source_setup']['config']['slack_channels']=='general'
 out=invoke(graph(calls),{**BASE,'request':'one_on_one','remove_sources':'Slack'},first['memory'])
 calls.clear();stale=invoke(graph(calls),{**BASE,'slack_channels':'general'},out['memory'])
 assert saved(stale)['source_setup']['config']['slack_channels']==''
 assert len(calls)==1 and calls[0][0][0]==('o/one',)
 assert 'slack:general:No' in saved(stale)['scope_sync']


def test_no_saved_report_automatically_reads_bounded_selected_scope():
 calls=[];out=invoke(graph(calls),{**BASE,'request':'review_material'})
 assert len(calls)==1 and out['result']['status']=='partial'
 assert out['result']['mode']=='review_material'


def test_normal_saved_only_report_never_refetches_completed_sources():
 calls=[];first=invoke(graph(calls),{**BASE,'slack_channels':'general'});calls.clear()
 out=invoke(graph(calls),{**BASE,'request':'one_on_one'},first['memory'])
 assert not calls and out['result']['status']=='completed'


def test_same_agent_new_graph_uses_saved_setup_and_independent_incremental_cursors():
 calls=[];first=invoke(graph(calls),{**BASE,'slack_channels':'general','jira_projects':'PAY'});calls.clear()
 out=invoke(graph(calls),{'request':'weekly_update','role_and_level':'Engineer'},first['memory'])
 assert len(calls)==3 and all(c[1].startswith('2026-10-13') for c in calls)
 assert saved(out)['source_setup']==saved(first)['source_setup']


def test_unknown_schema_and_oversize_fail_without_discarding_history():
 from store import pack
 calls=[]
 with pytest.raises(ValueError,match='Saved history'):
  invoke(graph(calls),BASE,{'profile':pack({'schema':999,'items':[{'id':'keep'}]})})
 assert not calls
 import random,string
 rng=random.Random(12);p={'items':[{'id':str(n),'text':''.join(rng.choices(string.ascii_letters,k=400))} for n in range(100)]}
 before=copy.deepcopy(p)
 with pytest.raises(ValueError,match='capacity'):NativeMemoryStore({},max_kb=1).dump(p)
 assert p==before


def test_malformed_new_repository_stops_before_fetch_or_state_change():
 calls=[];first=invoke(graph(calls),BASE);calls.clear();before=copy.deepcopy(first['memory'])
 with pytest.raises(ValueError,match='owner/repo'):
  invoke(graph(calls),{**BASE,'request':'one_on_one','repos_or_org':'o/one, bad'},first['memory'])
 assert not calls and first['memory']==before
