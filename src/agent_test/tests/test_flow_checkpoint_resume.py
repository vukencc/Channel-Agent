"""项目 Flow 检查点恢复的受控离线回归。"""
import asyncio
import copy

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.llm import ModelConnectionError
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


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
