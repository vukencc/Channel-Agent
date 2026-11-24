"""项目 Flow 检查点恢复的受控离线回归。"""
import asyncio
import copy
import json

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.llm import ModelConnectionError, TOOL_SCHEMAS
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import selected_schemas
from test_flow_draft_review_runtime import assistant, call, review_payload, runtime_store
from test_flow_receipt_handoff_runtime import ticket_tool


@pytest.mark.parametrize('legacy', [False, True])
def test_resume_same_run_skips_completed_nodes(tmp_path, monkeypatch, sandbox_env, legacy):
    monkeypatch.setattr(config, 'FLOW_AUTO_ARCHIVE', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'private')
    calls = []
    async def model(history, *, session_id, emit):
        session = manager.sessions[session_id]
        node = session.record['flow_node_id']
        calls.append((node, session_id))
        if node == 'b' and sum(name == 'b' for name, _ in calls) <= 2:
            raise ModelConnectionError('offline fixture')
        return {'role': 'assistant', 'content': '完成'}
    async def run():
        nonlocal manager
        manager = SessionManager(store, model=model)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        try:
            template = flows.create()
            template.update(nodes=[{'id': n, 'title': n, 'prompt': n} for n in ('a', 'b', 'c')],
                            edges=[{'source': 'a', 'target': 'b'}, {'source': 'b', 'target': 'c'}])
            template = flows.save(template)
            project = projects.create('项目', str(sandbox_env), flow_id=template['id'])
            fid = project['flow_id']
            flows.start(fid, input_text='输入')
            await asyncio.wait_for(flows.tasks[fid], 5)
            if legacy:
                legacy_run = flows.records[fid]['run']
                legacy_run.pop('definition_digest')
                legacy_run.pop('project_revision')
                for row in legacy_run['nodes'].values():
                    row.pop('session_configuration_digest', None)
                manager.sessions[legacy_run['nodes']['b']['session_id']].record.pop('flow_project_id')
                flows._write(flows.records[fid])
            before = flows.get(fid)['run']
            flows._resume_plan(flows.records[fid], before['id'])
            assert before['status'] == 'failed' and before['resume_available']
            first = copy.deepcopy(before['nodes']['a'])
            sid = before['nodes']['b']['session_id']
            session = manager.sessions[sid]
            with pytest.raises(ValueError):
                flows.resume(fid, run_id='wrong')
            original_turn = session.record['last_run']['turn_id']
            session.record['last_run']['turn_id'] = 'changed'
            assert not flows.get(fid)['run']['resume_available']
            session.record['last_run']['turn_id'] = original_turn
            session.record['budget_overrides'] = {'TOOL_TIMEOUT': 1}
            assert not flows.get(fid)['run']['resume_available']
            session.record.pop('budget_overrides')
            original_reason = session.record['last_run']['stop_reason']
            session.record['last_run']['stop_reason'] = 'context_budget'
            assert not flows.get(fid)['run']['resume_available']
            session.record['last_run']['stop_reason'] = original_reason
            session.cancelled.set()
            assert not flows.get(fid)['run']['resume_available']
            session.cancelled.clear()
            if not legacy:
                node = flows.records[fid]['nodes'][1]
                original_prompt = node['prompt']
                node['prompt'] = 'altered'
                assert not flows.get(fid)['run']['resume_available']
                node['prompt'] = original_prompt
            original_project = projects.records[project['id']]
            original_project['revision'] += 1
            assert not flows._snapshot(flows.records[fid])['run']['resume_available']
            original_project['revision'] -= 1
            with monkeypatch.context() as patch:
                patch.setattr(flows, '_write', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk')))
                with pytest.raises(OSError):
                    flows.resume(fid, run_id=before['id'])
            assert len(calls) == 2 and flows.get(fid)['run']['status'] == 'failed'
            flows.resume(fid, run_id=before['id'])
            with pytest.raises(ValueError):
                flows.resume(fid, run_id=before['id'])
            await asyncio.wait_for(flows.tasks[fid], 5)
            mid = flows.get(fid)['run']
            assert mid['status'] == 'failed' and mid['resume_available']
            assert mid['nodes']['c']['status'] == 'blocked'
            flows.resume(fid, run_id=before['id'])
            await asyncio.wait_for(flows.tasks[fid], 5)
            after = flows.get(fid)['run']
            assert after['id'] == before['id'] and after['status'] == 'completed'
            assert after['nodes']['a'] == first
            assert [name for name, _ in calls] == ['a', 'b', 'b', 'b', 'c']
            assert calls[2][1] == calls[3][1] == sid
            assert len(after['resume_attempts']) == 2
        finally:
            await manager.shutdown()
    manager = None
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_start_releases_reservation_if_run_metadata_fails(tmp_path, monkeypatch, sandbox_env):
    store = SessionStore(tmp_path / 'state', tmp_path / 'private')
    async def run():
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        try:
            template = flows.create()
            template['nodes'] = [{'id': 'a', 'title': 'a', 'prompt': 'a'}]
            template = flows.save(template)
            project = projects.create('项目', str(sandbox_env), flow_id=template['id'])
            monkeypatch.setattr(flows, '_new_run', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk')))
            with pytest.raises(OSError):
                flows.start(project['flow_id'])
            assert not manager.project_reservations
            assert project['flow_id'] not in flows.tasks
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


@pytest.mark.parametrize('failure', [TimeoutError, ModelConnectionError])
@pytest.mark.parametrize('checkpoint_stage, outcome', [
    ('pending', 'blocked'),
    ('pending', 'restaged'),
    ('signed', 'blocked'),
    ('signed', 'verified'),
    ('signed', 'duplicate'),
])
def test_resume_keeps_dynamic_action_handoff_constraints(
        runtime_store, monkeypatch, failure, checkpoint_stage, outcome):
    """断线后，未提交的草稿与已签发的编号仍约束同一 Flow 的交接。"""
    store, project = runtime_store
    monkeypatch.setattr(config, 'FLOW_AUTO_ARCHIVE', False)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 0)
    writes = []
    phases = []
    resume_calls = 0
    resumed = False
    action = {'feedback_id': 'CF-01', 'diagnosis': '页面显示空白'}
    contract = {'fields': {'submitted_ticket_ids': {'type': 'array'}}}

    async def model(history, *, session_id, emit):
        nonlocal resume_calls
        session = manager.sessions[session_id]
        if session.record['flow_node_id'] == 'next':
            return assistant('下游已收到交接')
        payload = review_payload(history)
        if payload is not None:
            return assistant('{"status":"pass"}')
        schemas = selected_schemas(TOOL_SCHEMAS)
        names = {row['function']['name'] for row in schemas}
        phases.append(names)
        if len(phases) == 1:
            preview = next(row['function']['name'] for row in schemas
                           if 'submit_ticket' in row['function'].get('description', ''))
            return assistant(calls=[call('preview', preview, **action)])
        if len(phases) == 2 and checkpoint_stage == 'signed':
            assert names == {'submit_ticket'}
            return assistant(calls=[call('commit', 'submit_ticket', **action)])
        if not resumed:
            raise failure('offline fixture')
        resume_calls += 1
        if resume_calls == 1:
            assert names != {'submit_ticket'}, '续跑不得恢复模型的旧批准'
            if outcome in {'restaged', 'duplicate'}:
                preview = next(row['function']['name'] for row in schemas
                               if 'submit_ticket' in row['function'].get('description', ''))
                return assistant(calls=[call('new-preview', preview, **action)])
        if outcome == 'restaged' and resume_calls == 2:
            assert names == {'submit_ticket'}
            return assistant(calls=[call('new-commit', 'submit_ticket', **action)])
        if outcome == 'duplicate':
            assert names != {'submit_ticket'}
        if outcome == 'blocked':
            if not names and checkpoint_stage == 'signed':
                return assistant('{"set":{"submitted_ticket_ids":["WRONG"]}}')
            return assistant('{"submitted_ticket_ids":["WRONG"]}')
        return assistant('{"submitted_ticket_ids":["ISSUED-1"]}')

    async def run():
        nonlocal manager, resumed
        def setup():
            manager = SessionManager(store, model=model)
            flows = FlowService(manager)
            projects = ProjectService(manager, flows)
            original_tool = manager._tool

            async def write_ticket(session, tool_call, *, evidence=None):
                if tool_call['function']['name'] != 'submit_ticket':
                    return await original_tool(session, tool_call, evidence=evidence)
                ledger = project / 'tickets.json'
                writes.append(json.loads(tool_call['function']['arguments']))
                ledger.write_text(json.dumps(writes), encoding='utf-8')
                evidence['source'] = 'executed'
                return '[退出码] 0\n[stdout]\n{"ok":true,"ticket":{"ticket_id":"ISSUED-1"}}'

            monkeypatch.setattr(manager, '_tool', write_ticket)
            return manager, flows, projects

        manager, flows, projects = setup()
        try:
            record = projects.create('办理续跑', str(project), permission_policy='full_access')
            fid = record['flow_id']
            flow = flows.get(fid)
            flow.update(nodes=[
                {'id': 'work', 'title': '办理', 'prompt': '提交工单，输出签发编号。', 'y': 0.0,
                 'tool_names': ['submit_ticket'], 'custom_tools': [ticket_tool()],
                 'handoff_contract': contract},
                {'id': 'next', 'title': '下游', 'prompt': '核对交接', 'x': 120.0, 'y': 0.0, 'tool_names': []},
            ], edges=[{'source': 'work', 'target': 'next'}])
            flows.save(flow)
            # 模拟旧版缺省 x 原样落盘，y 仍是调用者提供的浮点零。
            flows.records[fid]['nodes'][0]['x'] = 0
            flows.start(fid)
            await asyncio.wait_for(flows.tasks[fid], 5)
            before = flows.get(fid)['run']
            assert before['resume_available'], before['error']
            assert len(writes) == (checkpoint_stage == 'signed')
            sid = before['nodes']['work']['session_id']
            session = manager.sessions[sid]
            expected_checkpoint = copy.deepcopy(session.record['_flow_action_checkpoint'])
            exported = json.loads(store.export(session.record, 'json').read_text(encoding='utf-8'))
            assert '_flow_action_checkpoint' not in exported
            await manager.shutdown()
            manager, flows, projects = setup()
            flows._resume_plan(flows.records[fid], before['id'])
            assert flows.get(fid)['run']['resume_available']
            node = flows.records[fid]['nodes'][0]
            assert type(node['x']) is int and type(node['y']) is float
            node['x'] = 1.0
            assert not flows.get(fid)['run']['resume_available']
            node['x'] = 0
            assert manager.sessions[sid].record['_flow_action_checkpoint'] == json.loads(json.dumps(expected_checkpoint))
            resumed = True
            flows.resume(fid, run_id=before['id'])
            await asyncio.wait_for(flows.tasks[fid], 5)
            after = flows.get(fid)['run']
            assert after['id'] == before['id']
            assert after['nodes']['work']['session_id'] == sid
            session = manager.sessions[sid]
            assert '_flow_action_checkpoint' not in session.record
            if outcome == 'blocked':
                assert after['nodes']['next']['status'] == 'blocked'
                expected = 'flow_handoff_receipts' if checkpoint_stage == 'signed' else 'flow_action_expired'
                assert session.record['last_run']['stop_reason'] == expected
                assert len(writes) == (checkpoint_stage == 'signed')
            else:
                assert after['nodes']['next']['status'] == 'completed', after['nodes']['work']['error']
                assert len(writes) == 1
                if outcome == 'duplicate':
                    assert [event['status'] for event in session.record['last_run']['action_preflights']] == ['already_completed']
        finally:
            await manager.shutdown()

    manager = None
    asyncio.run(run())
