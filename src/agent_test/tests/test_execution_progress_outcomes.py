"""失败命令保留执行证据，但不能伪计为成功执行进展。"""
import asyncio
import json

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_failed_real_command_is_not_successful_execution_progress(tmp_path, sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 0)

    async def model(history, **kwargs):
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': '命令已失败，记录真实状态。'}
        return {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': 'failure', 'function': {'name': 'run_command',
                'arguments': json.dumps({'command': 'python3 -I -c "raise SystemExit(1)"'})}}]}

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        try:
            owner = manager.create()
            manager.submit(owner, '执行并检查命令')
            await owner.task
            metric, = owner.record['last_run']['tools']
            result = next(m for m in owner.record['messages'] if m['role'] == 'tool')
            assert result['tool_call_id'] == 'failure' and result['content'].startswith('[退出码] 1\n')
            assert metric['source'] == 'executed'
            assert metric['execution_progress'] is False
            assert metric['outcome'] == 'error'
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_recovery_allowance_resets_after_completed_model_round(tmp_path, sandbox_env, monkeypatch):
    from agent_core.core.llm import ModelResponseError
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 0)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 1)
    calls = []

    async def model(history, **kwargs):
        calls.append(1)
        if len(calls) in {1, 3}:
            raise ModelResponseError('受控生成未完整结束')
        if len(calls) == 5:
            return {'role': 'assistant', 'content': '两次独立恢复后完成。'}
        return {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': f'read-{len(calls)}', 'function': {'name': 'read_file',
                'arguments': '{"path":"input.txt"}'}}]}

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store, model=model)
        try:
            owner = manager.create()
            (manager.workspace(owner) / 'input.txt').write_text('真实工具读取内容')
            manager.submit(owner, '读取并核验')
            await owner.task
            assert owner.record['status'] == 'idle'
            assert len(calls) == 5
            assert len(owner.record['last_run']['tools']) == 2
            assert owner.record['last_run']['recovery_attempts'] == 2
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())
