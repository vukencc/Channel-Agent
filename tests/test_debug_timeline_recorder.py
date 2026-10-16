"""Causal metadata and incremental trace windows never expose private content."""
import concurrent.futures
import json
import math
from types import SimpleNamespace

import pytest

from ai_agent_startup.core import debug_runtime
from ai_agent_startup.core.debug_runtime import DebugRecorder


def test_cursor_window_reset_and_empty_delta():
    recorder = DebugRecorder(True, capacity=3)
    assert recorder.snapshot()['cursor'] == 0
    for index in range(5):
        assert recorder.event('phase.changed', session_id='session', stage_index=index) == index + 1
    full = recorder.snapshot()
    assert [row['seq'] for row in full['timeline']] == [3, 4, 5]
    assert full['oldest_seq'] == 3
    assert full['cursor'] == 5
    assert not full['incremental'] and not full['gap'] and not full['reset']
    delta = recorder.snapshot(cursor=3, generation=full['generation'])
    assert [row['seq'] for row in delta['timeline']] == [4, 5]
    assert delta['incremental'] and not delta['reset']
    assert recorder.snapshot(cursor=5, generation=full['generation'])['timeline'] == []
    missed = recorder.snapshot(cursor=1, generation=full['generation'])
    assert missed['gap'] and missed['reset'] and not missed['incremental']
    assert missed['timeline'] == full['timeline']
    restarted = recorder.snapshot(cursor=5, generation='old-generation')
    assert restarted['reset'] and not restarted['incremental']
    assert restarted['timeline'] == full['timeline']
    assert recorder.snapshot(cursor=6, generation=full['generation'])['reset']
    assert full['dropped'] == 2


def test_cursor_replay_has_stable_ids_and_no_duplicates():
    recorder = DebugRecorder(True)
    recorder.event('turn.started', session_id='session', turn_id='turn-1')
    first = recorder.snapshot()
    recorder.event('turn.stopped', session_id='session', turn_id='turn-1')
    delta = recorder.snapshot(cursor=first['cursor'], generation=first['generation'])
    combined = first['timeline'] + delta['timeline']
    assert [event['seq'] for event in combined] == [1, 2]
    assert combined == recorder.snapshot()['timeline']
    assert DebugRecorder(True).snapshot()['generation'] != first['generation']


def test_snapshot_clock_advances_without_publishing_events(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(debug_runtime.time, 'monotonic', lambda: clock[0])
    recorder = DebugRecorder(True)
    with recorder.scope('session'):
        with recorder.span('tool.execute'):
            first = recorder.snapshot()
            clock[0] += .25
            later = recorder.snapshot()
            assert first['now_elapsed_ms'] == 0
            assert later['now_elapsed_ms'] == 250
            assert later['cursor'] == first['cursor']
            assert later['revision'] == first['revision']
            assert later['timeline'] == first['timeline']
            assert later['active'][0]['duration_ms'] == 250
    disabled = DebugRecorder(False)
    clock[0] += .5
    snapshot = disabled.snapshot()
    assert snapshot['now_elapsed_ms'] == 500
    assert snapshot['timeline'] == [] and snapshot['cursor'] == 0


def test_threaded_publish_allocates_strictly_ordered_sequences():
    recorder = DebugRecorder(True, capacity=1000)
    def publish(index):
        return recorder.event('context.sample', session_id='session', metrics={'input_tokens': index})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(publish, range(200)))
    rows = recorder.snapshot()['timeline']
    assert sorted(sequences) == list(range(1, 201))
    assert [row['seq'] for row in rows] == list(range(1, 201))
    assert all(row['ts'] > 0 for row in rows)
    assert [row['elapsed_ms'] for row in rows] == sorted(row['elapsed_ms'] for row in rows)


def test_metadata_allowlist_rejects_secrets_invalid_ids_and_nonfinite_metrics():
    recorder = DebugRecorder(True)
    secret = 'SECRET canary / prompt https://private.invalid?api_key=secret'
    seq = recorder.event('model.completed', session_id='session', turn_id='turn-1',
                         state=secret, reason=secret, tool_name=secret, task_id=secret,
                         prompt=secret, reasoning=secret, command=secret, arguments=secret,
                         error=secret, url=secret, api_key=secret,
                         metrics={'input_tokens': 12, 'output_tokens': math.inf,
                                  'context_chars': math.nan, 'input_cost': .25,
                                  'tokens_source': 'estimate', 'prompt': secret})
    row = recorder.snapshot()['timeline'][0]
    assert seq == 1
    assert row['turn_id'] == 'turn-1'
    assert row['metrics'] == {'input_tokens': 12, 'input_cost': .25, 'tokens_source': 'estimated'}
    assert secret not in json.dumps(recorder.snapshot())
    assert not {'state', 'reason', 'tool_name', 'task_id'} & row.keys()
    assert recorder.event(secret, session_id='session') is None
    assert recorder.snapshot()['cursor'] == 1


def test_nested_snapshot_values_are_copies():
    recorder = DebugRecorder(True)
    metrics = {'input_tokens': 10}
    recorder.event('model.completed', session_id='session', metrics=metrics)
    metrics['input_tokens'] = 11
    snapshot = recorder.snapshot()
    snapshot['timeline'][0]['metrics']['input_tokens'] = 99
    assert recorder.snapshot()['timeline'][0]['metrics']['input_tokens'] == 10


def test_legacy_span_rows_also_validate_metadata_and_numeric_overflow():
    recorder = DebugRecorder(True)
    secret = 'SECRET prompt / https://private.invalid?key=secret'
    with recorder.scope(secret):
        with recorder.span('tool.execute', secret):
            assert secret not in json.dumps(recorder.snapshot())
    recorder.event('model.completed', metrics={'input_tokens': 10 ** 1000, 'output_tokens': True})
    assert 'metrics' not in recorder.snapshot()['timeline'][-1]
    assert secret not in json.dumps(recorder.snapshot())


def test_disabled_event_and_context_do_no_sanitization_or_clock_work(monkeypatch):
    recorder = DebugRecorder(False)
    def forbidden(*args, **kwargs):
        pytest.fail('disabled recorder did work')
    monkeypatch.setattr(debug_runtime.time, 'time', forbidden)
    monkeypatch.setattr(debug_runtime.time, 'monotonic', forbidden)
    with recorder.scope('session'):
        with debug_runtime.debug_context(turn_id='turn'):
            assert recorder.event('model.completed', session_id='session', metrics={'input_tokens': 1}) is None
            assert debug_runtime.debug_event('turn.started') is None
    assert recorder._revision == 0


def test_active_spans_keep_causal_ids_and_session_parent_isolation():
    recorder = DebugRecorder(True)
    with recorder.scope('parent', turn_id='turn-1'):
        with recorder.span('agent.run') as outer:
            with debug_runtime.debug_context(batch_id='batch-1', tool_call_id='call-1'):
                with recorder.span('tool.execute', 'read_file') as tool:
                    snapshot = recorder.snapshot(cursor=recorder.snapshot()['cursor'],
                                                 generation=recorder.snapshot()['generation'])
                    active = next(row for row in snapshot['active'] if row['id'] == tool.id)
                    assert active['turn_id'] == 'turn-1'
                    assert active['batch_id'] == 'batch-1'
                    assert active['tool_call_id'] == 'call-1'
                    assert active['span_id'] == tool.id and active['parent_span_id'] == outer.id
                    assert snapshot['timeline'] == []
                    with recorder.scope('child', task_id='task-1'):
                        with recorder.span('agent.run') as child:
                            child_row = next(row for row in recorder.snapshot()['active']
                                             if row['id'] == child.id)
                            assert child_row['parent_id'] is None
                            assert child_row.get('parent_span_id') is None
                            assert child_row.get('turn_id') is None
                            assert child_row.get('tool_call_id') is None
                            assert child_row['task_id'] == 'task-1'
                    assert debug_runtime.debug_event('tool.completed', state='completed') is not None
    rows = recorder.snapshot()['timeline']
    own = [row for row in rows if row.get('span_id') == tool.id and row['kind'].startswith('span.')]
    assert [row['kind'] for row in own] == ['span.started', 'span.completed']
    assert all(row['tool_call_id'] == 'call-1' for row in own)
    assert len(recorder.snapshot()['events']) == 3


def test_future_completion_keeps_origin_context():
    recorder = DebugRecorder(True)
    future = concurrent.futures.Future()
    with recorder.scope('session', turn_id='turn-1'):
        with recorder.span('agent.run') as parent:
            recorder.track_future(future, 'session', 'persistence.save')
    with recorder.scope('other', turn_id='other-turn'):
        future.set_result(None)
    finished = [row for row in recorder.snapshot()['timeline']
                if row['kind'] == 'span.completed' and row.get('module') == 'persistence.save'][0]
    assert finished['session_id'] == 'session'
    assert finished['turn_id'] == 'turn-1'
    assert finished['parent_span_id'] == parent.id


@pytest.mark.parametrize(('result', 'state'), [
    ('[退出码] 1\nprivate command body', 'error'),
    ('[退出码] -9\nprivate command body', 'error'),
    ('[退出码] 0\nprivate command body', 'completed'),
    ('normal body\n[退出码] 1', 'completed'),
    ('[退出码] 1 not a status line', 'completed'),
])
def test_tool_outcome_reads_only_precise_first_line_exit_status(result, state):
    assert debug_runtime.tool_outcome(result) == state


def test_tool_duration_is_finite_metadata():
    recorder = DebugRecorder(True)
    recorder.event('tool.completed', session_id='session', duration_ms=12.5)
    recorder.event('tool.completed', session_id='session', duration_ms=math.inf)
    rows = recorder.snapshot()['timeline']
    assert rows[0]['duration_ms'] == 12.5
    assert 'duration_ms' not in rows[1]


def test_traced_agent_start_uses_stored_turn_id():
    recorder = DebugRecorder(True)
    owner = SimpleNamespace(debug=recorder)
    session = SimpleNamespace(id='session', record={'last_run': {'turn_id': 'turn-1'}})
    @debug_runtime.traced('agent.run')
    async def run(_owner, _session):
        return None
    import asyncio
    asyncio.run(run(owner, session))
    assert all(row['turn_id'] == 'turn-1' for row in recorder.snapshot()['timeline'])
