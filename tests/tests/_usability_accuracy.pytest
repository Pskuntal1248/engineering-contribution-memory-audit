import asyncio,copy
import pytest
import analysis,source_setup,outputs,agent
from models import Item
from test_shared_source_setup import BASE,graph,invoke,saved


def test_legacy_exclusions_become_effective_repo_list_not_hidden_filter_or_expansion():
 prior={'source_setup':{'revision':3,'config':source_setup.canonical({**BASE,'repos_or_org':'o/one, o/two','exclude_repos':'o/two'})}}
 inp,setup=source_setup.resolve(prior,BASE)
 assert setup['config']['repos_or_org']=='o/one' and setup['config']['exclude_repos']=='' and inp['exclude_repos']==''


def test_problem_body_is_used_with_evidence_but_reverts_not_shipped():
 i=Item(id='pr1',source='github',type='pr',date='2026-10-10',title='PAY-1 Add idempotency key to payouts',text='PAY-1: payouts could be created twice when a client retried a timed-out call. Adds an idempotency key so a retry returns the first payout.',meta={'mine':True,'merged_flag':True})
 assert outputs.problem_evidence(i)
 summary=outputs.problem_summary(i)
 assert 'timed-out call' in summary and 'Adds an idempotency key' in summary
 ordinary=Item(id='p2',source='github',type='pr',date='2026-10-10',title='Add dashboard',text='Adds a dashboard with metrics.')
 assert not outputs.problem_evidence(ordinary)


def test_skill_count_does_not_use_epic_or_referenced_ticket_as_change_identity():
 a=Item(id='a',source='github',type='pr',date='2026-10-10',title='PAY-1 Add idempotency key to payouts',keys=['PAY-1','PAY-100'],meta={'mine':True})
 b=Item(id='b',source='github',type='pr',date='2026-10-10',title='PAY-2 Retry payouts with backoff',keys=['PAY-2','PAY-1','PAY-100'],meta={'mine':True})
 assert len(analysis._distinct_work([a,b]))==2
 sk={s['skill']:s for s in analysis.map_skills([a,b])}
 assert sk['payments & idempotency']['count']==2


def test_profile_slack_section_says_not_checked_when_not_selected():
 calls=[];out=invoke(graph(calls),BASE)
 section=next(s for s in out['corpus']['sections'] if s['heading']=='Invisible work')
 assert section['body'].startswith('Not checked: Slack') and 'None found' not in section['body']


def test_manifest_hides_revision_and_exclusions_and_retains_original_spec_inputs():
 import yaml
 m=yaml.safe_load(open('manifest.yaml'));names={i['name'] for i in m['inputs']}
 assert 'setup_revision' not in names and 'exclude_repos' not in names
 assert {'github_username','repos_or_org','jira_site','jira_projects','slack_channels','include_dms','docs_source','backfill_months','role_and_level','review_framework','timezone'}<=names



def test_report_selector_only_has_reports_and_removed_controls_stay_hidden():
 import yaml
 m=yaml.safe_load(open('manifest.yaml'));fields={i['name']:i for i in m['inputs']}
 assert fields['request']['options']==['weekly_update','weekly_summary','one_on_one','review_material','interview_stories','evidence_gaps']
 assert not {'setup_revision','exclude_repos','display_name'} & fields.keys()
 assert len(fields)==19


def test_current_github_search_includes_today_but_historical_boundary_is_exclusive():
 from sources import GatewaySource
 from guards import ToolLog
 calls=[]
 async def call(name,args):
  calls.append((name,args));return {'items':[]}
 src=GatewaySource(call,'me',['o/r'],[],False,[],{'github.search_issues','github.list_pull_requests'},[],[])
 src.configure_scope([],'2026-10-01T00:00:00Z','2026-10-14T12:00:00Z');src.window_is_current=True
 asyncio.run(src._github_repo({'github':{'prs':[],'reviews':{},'commits':[]},'failed':[],'capped':[]},'o/r','2026-10-01T00:00:00Z',ToolLog()))
 assert all('created:<2026-10-15' in a['q'] for n,a in calls if n=='github.search_issues')
 calls.clear();src.configure_scope([],'2026-10-01T00:00:00Z','2026-10-08T00:00:00Z');src.window_is_current=False
 asyncio.run(src._github_repo({'github':{'prs':[],'reviews':{},'commits':[]},'failed':[],'capped':[]},'o/r','2026-10-01T00:00:00Z',ToolLog()))
 assert all('created:<2026-10-08' in a['q'] for n,a in calls if n=='github.search_issues')


def test_notion_upper_bound_does_not_hide_next_page_or_claim_limited_listing_complete():
 import adapters
 from guards import ToolLog
 calls=[]
 async def call(name,args):
  calls.append((name,args))
  if name=='notion.list_users':return {'results':[{'id':'u','type':'person','person':{'email':'me@example.com'}}]}
  if name=='notion.search_notion_page':
   if not args.get('start_cursor'):return {'results':[{'id':'new','last_edited_time':'2026-10-10T00:00:00Z'}],'next_cursor':'next'}
   return {'results':[{'id':'old','last_edited_time':'2026-10-03T00:00:00Z','created_by':{'id':'u'},'last_edited_by':{'id':'u'}}]}
  if name=='notion.get_page_markdown':return {'markdown':'somebody fixed a fault'}
 call.window_end='2026-10-08T00:00:00Z'
 out={'docs':[],'failed':[],'capped':[]}
 asyncio.run(adapters.fetch_notion(call,'2026-10-01T00:00:00Z',ToolLog(),out,['me@example.com']))
 assert len([a for n,a in calls if n=='notion.search_notion_page'])==2
 assert [a['page_id'] for n,a in calls if n=='notion.get_page_markdown']==['old']
 assert not out['capped']
