"""真实归档流程的 DEBUG 采样不改变 Compact 判定或持久原文。"""
import asyncio
import copy

import pytest

from agent_core import config
from agent_test.runtime import DebugRecorder
from agent_core.core.history_context import HistoryContext
from agent_core.core.sessions import SessionManager
from test_compact_frequency import compact_case, SupervisorStub, _messages, _set_utilization


def test_real_compact_records_before_after_samples_and_no_raw_history(compact_case, monkeypatch):
    monkeypatch.setattr(config, 'DEBUG', True)

    async def run():
        manager = SessionManager(compact_case, observer=DebugRecorder(config.DEBUG))
        supervisor = SupervisorStub()
        session = manager.create()
        session.record['last_run'] = {'turn_id': 'compact-turn'}
        service = HistoryContext(manager, supervisor=supervisor)
        messages = _messages(count=2)
        messages[3]['content'] += 'sensitive-archived-evidence ' * 30
        original = copy.deepcopy(messages)
        try:
            monkeypatch.setattr(config, 'CONTEXT_SUMMARY_CHARS', 32)
            _set_utilization(monkeypatch, service, session, messages, .93)
            _, result = await service.prepare(session, messages, schemas=[])
            assert result['compacted']
            timeline = manager.debug.snapshot()['timeline']
            before = next(row for row in timeline if row['kind'] == 'context.sample'
                          and row.get('reason') == 'before_compact')
            after = next(row for row in timeline if row['kind'] == 'context.sample'
                         and row.get('reason') == 'after_compact')
            compacted = next(row for row in timeline if row['kind'] == 'context.compacted')
            assert before['seq'] < after['seq'] < compacted['seq']
            assert before['metrics']['context_utilization'] == pytest.approx(result['before_utilization'])
            assert after['metrics']['context_utilization'] == pytest.approx(result['utilization'])
            assert after['metrics']['context_utilization'] <= config.CONTEXT_COMPACT_TARGET_RATIO
            assert all(row['turn_id'] == 'compact-turn' for row in (before, after, compacted))
            assert messages == original
            assert 'sensitive-archived-evidence' not in str(timeline)
            assert len(supervisor.inspections) == 1
            assert len([kind for kind, _ in supervisor.summaries if kind == 'session']) == 1
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_plan_stage_switch_uses_step_numbers_without_private_titles(compact_case, monkeypatch):
    monkeypatch.setattr(config, 'DEBUG', True)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)

    async def run():
        manager = SessionManager(compact_case, observer=DebugRecorder(config.DEBUG))
        session = manager.create()
        try:
            await manager.flush()
            plan = await manager.task_plans.create(session, 'private goal', [
                {'key': 'first', 'title': 'private first title', 'acceptance': 'check first', 'depends_on': []},
                {'key': 'second', 'title': 'private second title', 'acceptance': 'check second', 'depends_on': []}])
            plan = await manager.task_plans.update(session, plan['plan_id'], 'first', 'in_progress',
                                                  expected_revision=plan['revision'])
            plan = await manager.task_plans.update(session, plan['plan_id'], 'first', 'blocked',
                block_reason='private blocked reason', expected_revision=plan['revision'])
            await manager.task_plans.update(session, plan['plan_id'], 'second', 'in_progress',
                                             expected_revision=plan['revision'])
            rows = [row for row in manager.debug.snapshot()['timeline'] if row['kind'] == 'plan.updated']
            assert [row['stage_index'] for row in rows] == [1, 1, 2]
            assert [row['state'] for row in rows] == ['running', 'checkpoint', 'running']
            assert {row['task_id'] for row in rows} == {plan['plan_id']}
            assert 'private' not in str(rows)
        finally:
            await manager.shutdown()
    asyncio.run(run())
