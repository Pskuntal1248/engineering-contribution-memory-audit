import asyncio
import pytest
from llm import GatewayLLM
from guards import Budget
from analysis import ThreadVerdicts
from report_notes import split_notes, FALLBACK

class Rejected(Exception):
    status_code = 402

class Fake:
    def __init__(self, failure): self.calls = 0; self.failure = failure
    def with_structured_output(self, schema): return self
    async def ainvoke(self, messages):
        self.calls += 1
        raise self.failure


def test_402_latches_without_more_attempts_or_budget_spends():
    llm = GatewayLLM('https://invalid.test', 'fake', backoff=0)
    llm._m = Fake(Rejected('private prompt / token / provider payload'))
    b = Budget(max_calls=10, max_seconds=60)
    for _ in range(3):
        with pytest.raises(RuntimeError, match='unavailable') as e:
            asyncio.run(llm.structured(ThreadVerdicts, 'test', {}, b))
        assert 'private' not in str(e.value)
    assert llm._m.calls == b.calls == 1
    assert llm.diagnostics == [{'status': 402, 'attempt': 1, 'model': llm.model}]


def test_transient_timeout_is_retried_but_bad_request_is_not():
    for error, count in [(TimeoutError('private'), 2), (ValueError('private'), 1)]:
        llm = GatewayLLM('https://invalid.test', 'fake', backoff=0)
        llm._m = Fake(error)
        with pytest.raises(RuntimeError): asyncio.run(llm.structured(ThreadVerdicts, 'test', {}))
        assert llm._m.calls == count


def test_notes_grouped_and_raw_model_errors_removed():
    info, skipped, failures = split_notes([
        'GitHub username taken from the connected GitHub account: example',
        'Jira site taken from the connected Jira account: https://example.test',
        'embeddings not re-run: there is no new evidence since the last run',
        'model call failed: private Error code: 402 after 2 attempts',
        'model call failed for decisions: private secret',
        'no review framework given: evidence gaps skipped; a framework would enable them'])
    assert len(info) == 3 and len(skipped) == 1 and failures == [FALLBACK]
    assert 'private' not in str((info, skipped, failures))


def test_rendered_corpus_and_result_notes_keep_plain_material_warning():
    from test_real_output import profile, WORK, FOUR
    from outputs import corpus_for, to_markdown
    items = WORK + FOUR
    prof = profile(items)
    corpus, result = corpus_for('interview_stories', prof, items, title='Test stories', terms=set(), keep_internal=True,
        colleagues=[], names={'Parth Singh'}, date_from='', date_to='', run_notes=[
        'model call failed: Error code: 402 PRIVATE', 'no review framework given: optional',
        'GitHub username taken from the connected GitHub account: example'], failed=[], new_ids=set(), quiet_line=None)
    text = to_markdown(corpus)
    assert FALLBACK in text and FALLBACK in result['notes']
    assert 'PRIVATE' not in text and 'model call failed' not in str(result)
    assert result['analysis_status'] == 'degraded'
    sections = {s['heading']: s for s in corpus['sections']}
    assert sections['What could not be done']['bullets'] == [FALLBACK]
    assert 'Run info' in sections and 'Skipped features' in sections
    assert 'Interview stories' in sections
