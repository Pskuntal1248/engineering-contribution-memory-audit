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
def run(g,inp,mem=None):return asyncio.run(g.ainvoke({'inputs':inp,'memory':mem or {}}))
def saved(out):return NativeMemoryStore(out['memory']).load()


def test_setup_update_enrich_then_stale_scheduled_inputs_use_latest_config():
 calls=[];g=graph(calls)
 first=run(g,BASE)
 assert len(calls)==1 and saved(first)['scope_sync']['github:o/one']['complete']
 calls.clear()
 update=run(g,{**BASE,'request':'update_source_setup','setup_revision':'1','slack_channels':'general'},first['memory'])
 assert not calls and update['result']['model_calls']==0
 assert saved(update)['source_setup']['revision']==2
 enrich=run(g,{**BASE,'request':'enrich_new_scopes'},update['memory'])
 assert len(calls)==1 and calls[0][0][2]==('general',) and not calls[0][0][0]
 assert enrich['result']['mode']=='enrichment'
 calls.clear()
 weekly=run(g,BASE,enrich['memory']) # original stale schedule has no Slack field
 assert len(calls)==2 and any(c[0][2]==('general',) for c in calls)
 assert saved(weekly)['source_setup']['revision']==2
 assert all(c[1].startswith('2026-10-13') for c in calls)


def test_on_demand_new_fields_do_not_implicitly_update_or_enrich():
 calls=[];g=graph(calls);first=run(g,BASE);calls.clear()
 out=run(g,{**BASE,'request':'one_on_one','slack_channels':'general'},first['memory'])
 assert not calls
 assert saved(out)['source_setup']['pending_config']['slack_channels']=='general'
 assert out['ctx']['source_setup']['config']['slack_channels']==''


def test_failed_scope_keeps_cursor_and_can_retry_without_github():
 calls=[];first=run(graph(calls),BASE)
 upd=run(graph(calls),{**BASE,'request':'update_source_setup','setup_revision':'1','slack_channels':'general'},first['memory'])
 calls.clear();bad=run(graph(calls,True),{**BASE,'request':'enrich_new_scopes'},upd['memory'])
 progress=saved(bad)['scope_sync']['slack:general:No']
 assert not progress['complete'] and not progress['cursor']
 assert saved(bad)['scope_sync']['github:o/one']['complete']
 calls.clear();good=run(graph(calls),{**BASE,'request':'enrich_new_scopes'},bad['memory'])
 assert len(calls)==1 and not calls[0][0][0]
 assert saved(good)['scope_sync']['slack:general:No']['complete']


def test_revision_replay_and_explicit_disable():
 calls=[];g=graph(calls);first=run(g,{**BASE,'slack_channels':'general'})
 out=run(g,{**BASE,'request':'update_source_setup','setup_revision':'1','slack_channels':''},first['memory'])
 assert not saved(out)['source_setup']['config']['slack_channels']
 with pytest.raises(ValueError,match='revision'):
  run(g,{**BASE,'request':'update_source_setup','setup_revision':'1','slack_channels':'general'},out['memory'])


def test_no_saved_output_refuses_no_backfill():
 calls=[]
 with pytest.raises(ValueError,match='No saved evidence'):
  run(graph(calls),{**BASE,'request':'interview_stories'})
 assert not calls


def test_first_all_source_setup_weekly_scopes_independent():
 calls=[];g=graph(calls);inp={**BASE,'slack_channels':'general, support','jira_projects':'PAY','docs_source':'Notion'}
 first=run(g,inp)
 assert len(calls)==5
 assert len(saved(first)['scope_sync'])==5
 assert all(p['complete'] for p in saved(first)['scope_sync'].values())
 calls.clear();run(g,BASE,first['memory'])
 assert len(calls)==5


def test_pending_sources_accumulate_without_retyping_and_enrich_only_new():
 calls=[];g=graph(calls);first=run(g,BASE);calls.clear()
 a=run(g,{**BASE,'request':'one_on_one','slack_channels':'general'},first['memory'])
 b=run(g,{**BASE,'request':'one_on_one','jira_projects':'PAY','jira_site':'https://jira.example'},a['memory'])
 c=run(g,{**BASE,'request':'one_on_one','docs_source':'Notion','repos_or_org':'o/one, o/two'},b['memory'])
 p=saved(c)['source_setup']['pending_config']
 assert p['slack_channels']=='general' and p['jira_projects']=='PAY' and p['docs_source']=='notion' and p['repos_or_org']=='o/one, o/two'
 assert not calls
 result=run(g,{**BASE,'request':'enrich_new_scopes'},c['memory'])
 assert len(calls)==4 and not any(c[0][0]==('o/one',) for c in calls)
 assert not saved(result)['source_setup'].get('pending_config')
 assert saved(result)['source_setup']['config']==p


def test_removed_scopes_cannot_be_staged_again_by_stale_schedule():
 calls=[];g=graph(calls);first=run(g,{**BASE,'slack_channels':'general','jira_projects':'PAY'})
 upd=run(g,{**BASE,'request':'update_source_setup','setup_revision':'1'},first['memory'])
 calls.clear();out=run(g,{**BASE,'slack_channels':'general','jira_projects':'PAY'},upd['memory'])
 assert not saved(out)['source_setup'].get('pending_config')
 assert len(calls)==1 and calls[0][0][0]==('o/one',)
 assert 'slack:general:No' in saved(out)['scope_sync']  # archive preserved


def test_weekly_failure_keeps_successful_cursor_and_next_week_retries_incremental():
 calls=[];inp={**BASE,'slack_channels':'general'}
 first=run(graph(calls),inp);before=saved(first)['scope_sync']['slack:general:No']['cursor'];calls.clear()
 bad=run(graph(calls,True),BASE,first['memory'])
 assert saved(bad)['scope_sync']['slack:general:No']['cursor']==before
 assert saved(bad)['scope_sync']['slack:general:No']['complete']
 assert saved(bad)['scope_sync']['slack:general:No']['status']=='incomplete'
 calls.clear();good=run(graph(calls),BASE,bad['memory'])
 assert len(calls)==2 and all(c[1].startswith('2026-10-13') for c in calls)
 assert saved(good)['scope_sync']['slack:general:No']['status']=='complete'


def test_new_graph_same_state_does_not_repeat_initial_backfill():
 calls=[];first=run(graph(calls),{**BASE,'slack_channels':'general','jira_projects':'PAY'})
 calls.clear();out=run(graph(calls),{'request':'weekly_update','role_and_level':'Engineer'},first['memory'])
 assert len(calls)==3 and all(c[1].startswith('2026-10-13') for c in calls)
 assert saved(out)['source_setup']==saved(first)['source_setup']


def test_setup_only_then_normal_run_does_not_implicitly_enrich():
 calls=[];g=graph(calls);first=run(g,{**BASE,'request':'update_source_setup'})
 assert not calls
 ordinary=run(g,BASE,first['memory'])
 assert not calls and ordinary['result']['pending_scopes']==['github:o/one']
 enrich=run(g,{**BASE,'request':'enrich_new_scopes'},ordinary['memory'])
 assert len(calls)==1 and saved(enrich)['scope_sync']['github:o/one']['complete']


def test_unknown_schema_and_oversize_memory_do_not_reset_or_drop_history():
 from store import pack
 calls=[];mem={'profile':pack({'schema':999,'items':[{'id':'keep'}]})}
 with pytest.raises(ValueError,match='Saved history'):
  run(graph(calls),BASE,mem)
 assert not calls and mem['profile']
 import random,string
 rng=random.Random(12)
 profile={'items':[{'id':str(n),'text':''.join(rng.choices(string.ascii_letters,k=400))} for n in range(100)]}
 prior=copy.deepcopy(profile)
 with pytest.raises(ValueError,match='capacity'):
  NativeMemoryStore({},max_kb=1).dump(profile)
 assert profile==prior


def test_malformed_pending_repository_does_not_save_or_fetch():
 calls=[];g=graph(calls);first=run(g,BASE);calls.clear()
 before=copy.deepcopy(first['memory'])
 with pytest.raises(ValueError,match='pending source setup'):
  run(g,{**BASE,'request':'one_on_one','repos_or_org':'o/one, bad'},first['memory'])
 assert not calls and first['memory']==before


def test_selected_jira_project_uses_own_identity_not_cross_linked_key():
 from models import Item
 i=Item(id='jira:OTHER-1',source='jira',type='ticket',date='2026-10-01',title='links selected ticket',keys=['OTHER-1','PAY-2'],url='https://jira.example/browse/OTHER-1',meta={'project':'OTHER'})
 cfg=setup.canonical({**BASE,'jira_projects':'PAY','jira_site':'https://jira.example'})
 assert not setup.in_view(i,cfg)
 i.meta['project']='PAY';i.url='https://different.example/browse/PAY-1'
 assert not setup.in_view(i,cfg)


def test_legacy_optional_selections_migrate_without_blanks_erasing_them():
 from store import NativeMemoryStore
 prior={'items':[],'last_run':'2026-10-01','backfill_done':True,'source_selection':{'jira':'PAY','slack':'general','docs':'Notion'},'repo_scope':{'mode':'allow-list','read':['o/one']},'cursors':{'github':'2026-10-01'},'seen':['archive']}
 calls=[];out=run(graph(calls),BASE,NativeMemoryStore({}).dump(prior))
 cfg=saved(out)['source_setup']['config']
 assert cfg['slack_channels']=='general' and cfg['jira_projects']=='PAY' and cfg['docs_source']=='Notion'
 assert not calls  # legacy shared cursor proves no per-scope complete sync, so enrichment must be explicit
 assert saved(out)['seen']==['archive'] and saved(out)['cursors']==prior['cursors']


def test_setup_replacement_does_not_require_retyping_saved_identity():
 calls=[];g=graph(calls);first=run(g,{**BASE,'slack_channels':'general','jira_site':'https://jira.example'})
 upd=run(g,{'request':'update_source_setup','setup_revision':'1','repos_or_org':'o/one','role_and_level':'Engineer'},first['memory'])
 cfg=saved(upd)['source_setup']['config']
 assert cfg['github_username']=='me' and cfg['jira_site']=='https://jira.example'
 assert cfg['slack_channels']==''
