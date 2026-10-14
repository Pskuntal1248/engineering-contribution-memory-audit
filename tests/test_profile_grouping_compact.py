"""Offline regressions. These fixtures are not the user's live report or an official run."""
import asyncio
import copy
import json

import pytest

from models import Item
from linking import link
from guards import budget_for, cap_role
import eprofile
import outputs

ME = 'Pskuntal1248'


def commit(n, repo, title='Initial commit', **meta):
    return Item(id=f'gh:commit:{repo}@{n}', source='github', type='commit', date='2026-10-10T10:00:00Z',
                title=title, text=title, url=f'https://github.com/{repo}/commit/{n}', author=ME,
                meta={'mine': True, 'repo': repo, 'paths': ['README.md'], **meta})


def build(items, prior=None, **kwargs):
    return asyncio.run(eprofile.build(items, username=ME, names={ME}, llm=None, budget=budget_for('backfill'),
                                     framework='', role_and_level='Engineer', prior=prior, **kwargs))


@pytest.mark.parametrize('dense', [None, 'identical'])
def test_generic_cross_repo_commits_stay_separate(dense):
    items = [commit(1, 'p/website'), commit(2, 'p/agents', 'first commit'), commit(3, 'p/database', 'Add README.md')]
    vectors = {i.id: [1., 0.] for i in items} if dense else None
    assert len(link(items, dense=vectors)) == 3
    assert not build(items)['projects']  # trivial items remain in ledger, not career projects


def test_loose_commit_does_not_attach_to_other_repo():
    items = [commit(1, 'p/one', 'Improve permission handling'), commit(2, 'p/one', 'Improve permission handling'), commit(3, 'p/two', 'Improve permission handling')]
    groups = link(items)
    assert sorted(len(g['item_ids']) for g in groups.values()) == [1, 2]


@pytest.mark.parametrize('relation', ['ticket', 'url', 'alias'])
def test_explicit_cross_repo_relationships_are_preserved(relation):
    a, b = commit(1, 'p/one', 'Improve permission handling'), commit(2, 'p/two', 'Improve permission handling')
    kwargs = {}
    if relation == 'ticket':
        a.keys = b.keys = ['PAY-1']
    elif relation == 'url':
        b.refs = [a.url]
    else:
        kwargs['repo_aliases'] = {'p/two': 'p/one'}
    assert len(link([a, b], **kwargs)) == 1


def test_jira_leftover_fold_cannot_cross_repo_without_exact_key():
    a, b = commit(1, 'p/one', 'SQL indexing'), commit(2, 'p/two', 'Mobile dictation')
    a.keys, b.keys = ['PAY-1'], ['PAY-2']
    assert len(link([a, b])) == 2


def test_refinement_does_not_force_a_below_threshold_unit():
    a, b = commit(1, 'p/one', 'SQL indexing'), commit(2, 'p/one', 'Mobile dictation')
    assert len(link([a, b], project_threshold=1.01)) == 2


def test_review_role_requires_own_review_evidence():
    authored = commit(1, 'p/one', 'Apply reviewed src changes')
    other = Item(id='r', source='github', type='review', author='other', date=authored.date, title='Review', meta={'mine': False})
    mine = Item(id='m', source='github', type='review', author=ME, date=authored.date, title='Review', meta={'mine': True})
    assert cap_role('reviewed', [authored], ME) == 'contributed'
    assert cap_role('reviewed', [authored, other], ME) == 'contributed'
    assert cap_role('reviewed', [other], ME) == 'supported'
    assert cap_role('built', [mine], ME) == 'reviewed'
    assert cap_role('reviewed', [authored, mine], ME) == 'reviewed'


def test_stale_grouping_cache_rebuilt_without_restoring_bad_assignments():
    items = [commit(1, 'p/one', 'Add permission route'), commit(2, 'p/one', 'Fix permission route'), commit(3, 'p/two', 'Add permission route')]
    prior = {'items': [eprofile.compact(i, 'old-bad-project') for i in items],
             'cards': [{'ev': [__import__('weekly').id_key(i.id) for i in items], 'name': 'Wrong mixed project', 'goal': 'Wrong', 'outcome': 'Wrong'}],
             'seen': ['preserved'], 'cursors': {'github': 'preserved'}}
    original = copy.deepcopy(prior)
    prof = build([eprofile.expand(i) for i in prior['items']], prior, reuse_names=True)
    assert prior == original
    assert all(p['name'] != 'Wrong mixed project' for p in prof['projects'])
    assert all(len({next(i for i in items if i.id == e).meta['repo'] for e in p['evidence']}) == 1 for p in prof['projects'])
    assert any('rebuilt from saved evidence' in n for n in prof['notes'])


def test_current_saved_group_restoration_cannot_cross_repo():
    items = [commit(1, 'p/one', 'Add permission route'), commit(2, 'p/one', 'Fix permission route'), commit(3, 'p/two', 'Database optimizer')]
    for i in items:
        i.meta['saved_project'] = 'old'
    prof = build(items, {'grouping_version': eprofile.GROUPING_VERSION, 'items': [{}]}, reuse_names=True)
    assert items[2].id not in prof['project_of']


def representative():
    items = [commit(f'{r}{n}', f'p/repo{r}', f'Implement feature{r} iteration {n}') for r in range(12) for n in range(8)]
    for i in items:
        i.meta['paths'] = [i.meta['repo'] + '/worker.py']
    return items, build(items)


def render(kind, prof, items):
    return outputs.corpus_for(kind, copy.deepcopy(prof), items, title='Offline fixture preview', terms=set(), keep_internal=False,
                              colleagues=[], names={ME}, date_from='', date_to='', run_notes=[], failed=[], quiet_line=None,
                              new_ids={i.id for i in items})


@pytest.mark.parametrize('kind', ['backfill', 'profile', 'weekly', 'weekly_summary', 'one_on_one', 'review_material', 'interview_stories', 'evidence_gaps'])
def test_every_output_preserves_complete_csv_and_compact_evidence(kind, tmp_path):
    items, prof = representative()
    corpus, result = render(kind, prof, items)
    tables = [s for s in corpus['sections'] if s.get('table')]
    assert len(tables) == 1 and len(tables[0]['table']['rows']) == len(items)
    assert corpus['sections'][-1] is tables[0]
    assert result['presentation']['evidence_links_per_block'] == 3
    if kind in {'profile', 'backfill'}:
        projects = next(s for s in corpus['sections'] if s['heading'] == 'Projects')['body']
        assert projects.count('https://') <= 24
        assert 'Showing 8 of 12' in str(corpus)
    if kind == 'review_material':
        section = corpus['sections'][0]['body']
        assert section.count('https://') <= len(prof['projects']) * 3
        assert all(p['name'] in section for p in prof['projects'])
    outputs.write_local_copy(corpus, str(tmp_path), result)
    assert (tmp_path / 'last_ledger.csv').read_text().count('\n') == len(items) + 1
    assert '| source |' not in (tmp_path / 'last_output.md').read_text()
    assert '| source |' in (tmp_path / 'last_full_report.md').read_text()


def test_platform_marker_preserves_grouping_version():
    import store
    from langgraph.store.memory import InMemoryStore
    backend = InMemoryStore()
    platform = store.PlatformStore(backend, {})
    mem = asyncio.run(platform.adump({'items': [], 'grouping_version': eprofile.GROUPING_VERSION}))
    assert mem['marker']['grouping_version'] == eprofile.GROUPING_VERSION


def test_loose_repoless_bridge_cannot_merge_disjoint_repos_in_refinement():
    a, b = commit(1, 'p/one', 'Permission route'), commit(2, 'p/two', 'Permission route')
    doc = Item(id='doc', source='notion', type='doc', date=a.date, title='Permission route', text='Permission route')
    groups = link([a, b, doc], dense={i.id: [1., 0.] for i in [a,b,doc]})
    assert all(not ({a.id,b.id} <= set(g['item_ids'])) for g in groups.values())


def test_model_cannot_borrow_another_projects_review_evidence():
    import analysis
    own = commit(1, 'p/one', 'Implement storage')
    other = Item(id='review', source='github', type='review', author=ME, date=own.date, title='Review storage', meta={'mine': True})
    base = [analysis.baseline_project_card('p', [own.id], {own.id:own}, ME)]
    class Model:
        async def structured(self, schema, system, payload, budget):
            return schema(projects=[{'project_id':'p', 'name':'Storage', 'goal':'not stated', 'outcome':'not stated', 'role':'reviewed', 'evidence':[other.id]}])
    cards,_ = asyncio.run(analysis.project_cards_with_model(Model(), budget_for('backfill'), base, {own.id:own,other.id:other}, ME))
    assert cards[0]['role'] == 'contributed'
    assert cards[0]['evidence'] == [own.id]

