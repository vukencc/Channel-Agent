"""RAG debug spans follow actual work without changing retrieval results."""
from copy import deepcopy

import pytest

from ai_agent_startup.core.debug_runtime import DebugRecorder
from ai_agent_startup.rag import tool


PHASES = ['rag.index', 'rag.vector', 'rag.bm25', 'rag.rrf', 'rag.rerank']
PRIVATE_QUERY = 'private query'
PRIVATE_DOCUMENT = 'private document'


@pytest.fixture
def rig(monkeypatch):
    recorder = DebugRecorder(enabled=True)
    observed = []
    failure = {'phase': None}

    def mark(phase):
        active = recorder.snapshot()['active']
        observed.append((phase, [row['module'] for row in active]))
        if failure['phase'] == phase:
            raise RuntimeError(f'{phase} private failure')

    parents = [
        {'id': 'a', 'document': PRIVATE_DOCUMENT,
         'metadata': {'filename': 'a.txt', 'start': 0, 'end': 16}},
        {'id': 'b', 'document': 'second document',
         'metadata': {'filename': 'b.txt', 'start': 0, 'end': 15}},
    ]

    class Index:
        children = []
        vector_backend = 'test'

        def __init__(self):
            self.parents = parents
            self.by_id = {row['id']: row for row in parents}

        def dense(self, query, limit, **options):
            mark('rag.vector')
            return [{'id': 'a', 'score': 0.8}]

        def bm25(self, query, limit, **options):
            mark('rag.bm25')
            return [{'id': 'b', 'score': 1.2}]

    index = Index()

    def get_index(**kwargs):
        mark('rag.index')
        return index

    original_rrf = tool.reciprocal_rank_fusion

    def rrf(*args, **kwargs):
        mark('rag.rrf')
        return original_rrf(*args, **kwargs)

    class Reranker:
        def score(self, query, documents):
            mark('rag.rerank')
            assert query == PRIVATE_QUERY
            assert set(documents) == {PRIVATE_DOCUMENT, 'second document'}
            return [2.0, 1.0]

    monkeypatch.setattr(tool, 'get_index', get_index)
    monkeypatch.setattr(tool, 'reciprocal_rank_fusion', rrf)
    monkeypatch.setattr(tool, 'get_reranker', Reranker)
    return recorder, observed, failure


def search(trace):
    return tool.rag_search(PRIVATE_QUERY, strictness='loose', trace=trace,
                           thresholds={'strict': 1.5, 'normal': 1.0, 'loose': 0.0})


def without_timings(trace):
    result = deepcopy(trace)
    del result['timings_seconds']
    return result


def test_phase_spans_follow_calls_and_preserve_trace(rig):
    recorder, observed, _ = rig
    baseline = {}
    baseline_result = search(baseline)
    assert [phase for phase, _ in observed] == PHASES
    assert all(not active for _, active in observed)

    observed.clear()
    trace = {}
    with recorder.scope('session-one'):
        result = search(trace)
    assert [phase for phase, _ in observed] == PHASES
    assert [active for _, active in observed] == [[phase] for phase in PHASES]
    assert result == baseline_result
    assert without_timings(trace) == without_timings(baseline)
    assert trace['timings_seconds'].keys() == baseline['timings_seconds'].keys()
    events = recorder.snapshot()['events']
    assert [event['module'] for event in events] == PHASES
    assert all(event['state'] == 'completed' and event['name'] == '' for event in events)
    assert PRIVATE_QUERY not in str(events)
    assert PRIVATE_DOCUMENT not in str(events)


@pytest.mark.parametrize('phase', PHASES)
def test_phase_errors_are_recorded_and_propagated(rig, phase):
    recorder, observed, failure = rig
    failure['phase'] = phase
    trace = {'unchanged': True}
    with recorder.scope('session-one'):
        with pytest.raises(RuntimeError, match=f'{phase} private failure'):
            search(trace)
    index = PHASES.index(phase)
    assert [name for name, _ in observed] == PHASES[:index + 1]
    events = recorder.snapshot()['events']
    assert [event['module'] for event in events] == PHASES[:index + 1]
    assert [event['state'] for event in events] == ['completed'] * index + ['error']
    assert events[-1]['error_type'] == 'RuntimeError'
    assert 'private failure' not in str(events)
    assert trace == {'unchanged': True}


def test_disabled_recorder_emits_no_phase_events(rig):
    recorder, observed, _ = rig
    recorder.enabled = False
    trace = {}
    with recorder.scope('session-one'):
        result = search(trace)
    assert [phase for phase, _ in observed] == PHASES
    assert all(not active for _, active in observed)
    assert '命中 2/2' in result
    assert recorder.snapshot()['events'] == []
    assert recorder.snapshot()['active'] == []
