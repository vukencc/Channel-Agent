import asyncio
import json

import config
from core.storage import SessionStore


def test_headless_text_response_and_resume_are_persisted(tmp_path):
    from core.headless import run_headless
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': '纯文本测试桩'}
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        try:
            first = await run_headless(store, '第一轮', model=model)
            second = await run_headless(store, '第二轮', session_id=first['session_id'][:8], model=model)
            assert first['exit_code'] == second['exit_code'] == 0
            assert second['messages'][0]['content'] == '第二轮'
            assert second['session_id'] == first['session_id']
            assert len(store.read_record(store.directory(first['session_id']) / 'session.json')['messages']) == 5
        finally:
            store.close()
    asyncio.run(run())


def test_headless_cancel_saves_state_and_returns_cancel_status(tmp_path):
    from core.headless import run_headless
    async def run():
        entered = asyncio.Event()
        async def model(history, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        try:
            task = asyncio.create_task(run_headless(store, '开始', model=model))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            result = await task
            assert result['exit_code'] == 130 and result['status'] == 'cancelled'
            assert store.read_record(store.directory(result['session_id']) / 'session.json')['status'] == 'cancelled'
        finally:
            store.close()
    asyncio.run(run())


def test_headless_error_output_hides_configured_credentials(tmp_path, monkeypatch):
    from core.headless import run_headless
    monkeypatch.setattr(config, 'API_KEY', 'synthetic-secret-for-unit-test')
    async def model(history, **kwargs):
        raise ValueError('服务拒绝 synthetic-secret-for-unit-test')
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        try:
            result = await run_headless(store, '开始', model=model)
            assert result['exit_code'] == 4
            assert 'synthetic-secret-for-unit-test' not in json.dumps(result)
        finally:
            store.close()
    asyncio.run(run())


def test_headless_denies_writes_without_tty_even_if_environment_is_trusted(tmp_path, monkeypatch):
    from core.headless import run_headless
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'trusted')
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [
        {'tool': 'create_file', 'path_prefix': '.', 'command_prefix': None}])
    async def model(history, **kwargs):
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': history[-1]['content']}
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'write', 'function': {'name': 'create_file', 'arguments': '{"path":"no.txt"}'}}]}
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        try:
            result = await asyncio.wait_for(run_headless(store, '保存', model=model), 2)
            assert not (store.workspace_path(result['session_id']) / 'no.txt').exists()
            tool = next(row for row in result['messages'] if row['role'] == 'tool')
            assert tool['tool_call_id'] == 'write' and '取消' in tool['content']
        finally:
            store.close()
    asyncio.run(run())


def test_headless_checkpoint_status_is_not_success(tmp_path, monkeypatch):
    from core.headless import run_headless
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 1)
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'read', 'function': {'name': 'list_files', 'arguments': '{}'}}]}
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        try:
            result = await run_headless(store, '继续', model=model)
            assert result['status'] == 'checkpoint' and result['exit_code'] == 3
            json.dumps(result, ensure_ascii=False)
        finally:
            store.close()
    asyncio.run(run())


def test_real_sigint_returns_json_and_persisted_cancelled_state(tmp_path):
    import os
    import select
    import signal
    import subprocess
    import sys
    script = '''
import asyncio, json, sys
from pathlib import Path
from core.headless import run_headless
from core.storage import SessionStore
async def model(history, **kwargs):
    print('READY', file=sys.stderr, flush=True)
    await asyncio.Event().wait()
root = Path(sys.argv[1])
store = SessionStore(root / 'state', root / 'workspace')
try:
    result = asyncio.run(run_headless(store, '合成 SIGINT 测试', model=model))
    print(json.dumps(result), flush=True)
finally:
    store.close()
raise SystemExit(result['exit_code'])
'''
    process = subprocess.Popen([sys.executable, '-u', '-c', script, str(tmp_path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        readable, _, _ = select.select([process.stderr], [], [], 10)
        assert readable and process.stderr.readline().strip() == 'READY'
        os.kill(process.pid, signal.SIGINT)
        output, error = process.communicate(timeout=5)
        assert process.returncode == 130, error
        result = json.loads(output)
        assert result['status'] == 'cancelled'
        path = tmp_path / 'state' / result['session_id'] / 'session.json'
        assert json.loads(path.read_text())['status'] == 'cancelled'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
