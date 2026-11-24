"""Flow 检查点展示使用受控字段，不传播模型正文。"""
from agent_core.core.flows import _checkpoint_message
import asyncio

from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_context_budget_shows_only_actual_overage_including_reserve():
    run = {'stop_reason': 'context_budget', 'context': {
        'sent_tokens': 17433, 'reserved_tokens': 2000, 'input_limit_tokens': 16000,
        'total_chars': 52718, 'reserved_chars': 8000, 'input_limit_chars': 64000,
        'message': 'SECRET_PROVIDER_TEXT'}}
    message = _checkpoint_message(run)
    assert '19433' in message and '16000' in message
    assert '60718' not in message and '64000' not in message
    assert 'SECRET_PROVIDER_TEXT' not in message
    assert '产物' in message and '模型管理' in message and '拆' in message


def test_context_budget_char_overage_and_untrusted_metrics():
    run = {'stop_reason': 'context_budget', 'context': {
        'sent_tokens': 100, 'reserved_tokens': 2, 'input_limit_tokens': 1000,
        'total_chars': 63000, 'reserved_chars': 2000, 'input_limit_chars': 64000}}
    message = _checkpoint_message(run)
    assert '65000' in message and '64000' in message
    assert '1000' not in message
    for bad in (None, True, -1, 'SECRET_PROVIDER_TEXT', 10**100):
        run['context']['total_chars'] = bad
        message = _checkpoint_message(run)
        assert 'SECRET_PROVIDER_TEXT' not in message and '65000' not in message
        assert '上下文预算' in message


def test_review_checkpoints_never_expose_model_output():
    for reason in ('flow_progress_review_blocked', 'flow_progress_review_invalid'):
        message = _checkpoint_message({'stop_reason': reason, 'context': 'SECRET_PROVIDER_TEXT'})
        assert '检查点' in message and 'SECRET_PROVIDER_TEXT' not in message


def test_flow_error_uses_bounded_context_details(tmp_path, sandbox_env):
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'private')
    async def run():
        manager = SessionManager(store)
        async def checkpoint(session, *args):
            session.record['status'] = 'checkpoint'
            session.record['last_run'].update(stop_reason='context_budget', context={
                'sent_tokens': 17433, 'reserved_tokens': 2000, 'input_limit_tokens': 16000,
                'total_chars': 52718, 'reserved_chars': 8000, 'input_limit_chars': 64000,
                'message': 'SECRET_PROVIDER_TEXT'})
        manager._run = checkpoint
        service = FlowService(manager)
        try:
            flow = service.create(workspace_path=str(sandbox_env), permission_policy='readonly')
            flow['nodes'] = [{'id': 'a', 'title': '测试', 'prompt': '核对'}]
            flow = service.save(flow)
            service.start(flow['id'], input_text='开始')
            await asyncio.wait_for(service.tasks[flow['id']], 5)
            result = service.get(flow['id'])['run']
            assert result['status'] == 'failed'
            assert '19433' in result['error'] and '16000' in result['error']
            assert 'SECRET_PROVIDER_TEXT' not in result['error']
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()
