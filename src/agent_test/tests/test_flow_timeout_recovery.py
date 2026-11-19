"""受控离线超时及工具能力提示，不调用真实模型。"""
import asyncio
import json
import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.prompts import runtime_file_workflow_guide
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_scoped_guidance_uses_available_write_tools():
    names = {'read_file', 'list_files', 'run_command', 'create_file', 'edit_file'}
    guide = runtime_file_workflow_guide(names, scoped=True)
    assert '2000' in guide and '独立' in guide and 'edit_file' in guide
    assert 'append_file' not in guide
    read_only = runtime_file_workflow_guide({'read_file', 'list_files'}, scoped=True)
    assert all(name not in read_only for name in ('create_file', 'edit_file', 'append_file', 'run_command'))


@pytest.mark.parametrize('archive', [False, True])
def test_flow_timeout_recovery_preserves_checkpoint(tmp_path, monkeypatch, sandbox_env, archive):
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 1)
    monkeypatch.setattr(config, 'FLOW_AUTO_ARCHIVE', archive)
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'private')
    seen = []
    async def model(history, **kwargs):
        seen.append(history)
        if len(seen) == 1:
            return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'write', 'type': 'function',
                'function': {'name': 'create_file', 'arguments': json.dumps({'path': 'evidence.txt', 'content': 'saved'})}}]}
        raise TimeoutError('PRIVATE_PROVIDER_TEXT')
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        try:
            flow = service.create(workspace_path=str(sandbox_env), permission_policy='full_access')
            flow['nodes'] = [{'id': 'a', 'title': '测试', 'prompt': '编写测试',
                              'tool_names': ['read_file', 'list_files', 'run_command', 'create_file', 'edit_file']}]
            flow = service.save(flow)
            service.start(flow['id'], input_text='完成测试')
            await asyncio.wait_for(service.tasks[flow['id']], 5)
            result = service.get(flow['id'])['run']
            session = manager.sessions[result['nodes']['a']['session_id']]
            assert len(seen) == 3
            assert any('恢复要求' in (m.get('content') or '') and '2000' in m['content']
                       and 'edit_file' in m['content'] for m in seen[2])
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] == 'model_timeout'
            assert '超时' in result['error'] and 'PRIVATE_PROVIDER_TEXT' not in result['error']
            assert len(session.record['last_run']['tools']) == 1
            assert session.record['last_run']['tools'][0]['outcome'] == 'completed'
            assert (sandbox_env / 'evidence.txt').read_text() == 'saved'
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()
