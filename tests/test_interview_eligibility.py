from models import Item
from interview import solved_text, authored_text
from outputs import stories, corpus_for, to_markdown
from eprofile import compact, expand


def thread(text, author='Parth Singh'):
    return Item(id='slack:C1:1',source='slack',type='thread',date='2026-10-10',title='Debug thread',text=text,author=author,url='https://example.invalid/thread',meta={'mine':True,'my_messages':2})


def minimal(items, inv=(), decisions=(), projects=()):
    return {'invisible':list(inv),'decisions':list(decisions),'projects':list(projects),'confidential':[]}


def test_cached_model_label_cannot_turn_a_question_into_a_solved_bug():
    it=thread('Parth Singh: I keep getting a null pointer, not sure how to debug it.\nOther: add a log and reproduce it.')
    p=minimal([it],inv=[{'label':'problem_solved','summary':'I fixed it','evidence':[it.id]}])
    assert stories(p,{it.id:it},set(),False,{'Parth Singh'})==[]
    assert authored_text(thread('Other: I fixed it.'))==''


def test_attributed_resolution_survives_new_memory_and_redaction():
    it=thread('Other: I need help.\nParth Singh: I traced the retry bug and fixed the window. The job passed.')
    saved=expand(compact(it,'p'))
    assert 'I traced' in solved_text(saved,{'Parth Singh'})
    p=minimal([saved],inv=[{'label':'problem_solved','summary':'cached label','evidence':[saved.id]}])
    st=stories(p,{saved.id:saved},{'Parth Singh'},False,{'Parth Singh'})
    assert len(st)==1 and 'My action: I traced' in st[0]['story'] and 'Other:' not in st[0]['story']


def test_proposed_adr_is_not_a_decision_made_even_with_cached_verdict():
    it=Item(id='gh:pr:9',source='github',type='pr',date='2026-10-10',title='ADR proposal',text='Proposes shared queue. Needs a decision this quarter.',author='Parth Singh',meta={'mine':True,'docs_only':True})
    d={'title':it.title,'role':'led','summary':'We decided to use a queue.','evidence':[it.id]}
    st=stories(minimal([it],decisions=[d]),{it.id:it},set(),True,{'Parth Singh'})
    assert st[0]['title']=='A design proposal' and 'acceptance is not established' in st[0]['story']
    # Cached wording must not be reused as an accepted fact in a proposal draft.
    assert 'decision you made' not in st[0]['question']


def test_delivery_filters_rollback_and_dedup_uses_evidence_not_counts():
    items=[];projects=[]
    for n in range(3):
        it=Item(id=f'gh:pr:{n}',source='github',type='pr',date='2026-10-10',title='Work',meta={'mine':True,'merged_flag':True})
        items.append(it);projects.append({'name':f'Project{n}','role':'contributed','outcome':'1 PR shipped.','evidence':[it.id]})
    rev=Item(id='gh:pr:7',source='github',type='pr',date='2026-10-10',title='Rolled back',meta={'mine':True,'merged_flag':True,'reverted':True})
    projects[0]['evidence'].insert(0,rev.id);items.append(rev)
    st=stories(minimal(items,projects=projects),{it.id:it for it in items},set(),True)
    assert len(st)==3 and all(rev.id not in s['evidence'] for s in st)
    assert all('end to end' not in s['question'] for s in st)
    assert 'Rolled-back' in st[0]['story']


def test_disagreement_requires_attributed_resolution():
    it=thread('Parth Singh: I disagreed with the queue proposal. We agreed to test both options.')
    p=minimal([it],inv=[{'label':'decision','summary':'cached','evidence':[it.id]}])
    st=stories(p,{it.id:it},set(),True,{'Parth Singh'})
    assert any(s['title']=='A disagreement' for s in st)
    it.text='Parth Singh: I disagreed with the queue proposal.'
    assert not stories(p,{it.id:it},set(),True,{'Parth Singh'})


def test_explicit_display_name_wins_over_connected_name():
    from test_identity import run
    from test_graph import INP
    async def who():return {'login':'Pskuntal1248','name':'Other GitHub name'}
    out=run({**INP,'github_username':'','display_name':'Typed Name'},who)
    assert out['inputs']['display_name']=='Typed Name'
    assert 'connected GitHub account: Pskuntal1248' in str(out['result']['notes'])
    assert 'display name entered in the form' not in str(out['result']['notes'])
