from outputs import corpus_for
from test_real_output import profile, WORK, FOUR
from models import Item


def test_proposal_not_counted_as_established_even_with_cached_accepted_summary():
    items=WORK+FOUR
    p=profile(items)
    it=Item(id='doc:adr',source='github',type='doc',date='2026-10-10',title='ADR',text='short saved text',meta={'needs_decision':True,'interview_proposed':True,'decision_text':True})
    items=items+[it]
    p['decisions']=[{'title':'ADR','role':'led','summary':'We decided to use a queue.','evidence':[it.id]}]
    c,r=corpus_for('interview_stories',p,items,title='test',terms=set(),keep_internal=True,colleagues=[],names=set(),date_from='',date_to='',run_notes=[],failed=[],quiet_line=None,new_ids=set())
    assert '0 established decisions; 1 design proposal' in c['summary']
    assert r['decisions']==0 and r['design_proposals']==1 and r['decision_records']==1
    it.meta={};it.text='Design record, acceptance unclear.'
    c,r=corpus_for('profile',p,items,title='test',terms=set(),keep_internal=True,colleagues=[],names=set(),date_from='',date_to='',run_notes=[],failed=[],quiet_line=None,new_ids=set())
    assert r['decisions']==0 and r['unconfirmed_design_records']==1
    it.text='We decided on a queue.'
    c,r=corpus_for('weekly',p,items,title='test',terms=set(),keep_internal=True,colleagues=[],names=set(),date_from='',date_to='',run_notes=[],failed=[],quiet_line=None,new_ids={it.id})
    assert r['decisions']==1 and '1 established decision; 0 design proposals' in c['summary']
