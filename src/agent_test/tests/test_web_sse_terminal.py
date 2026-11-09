"""用受控模型和真实会话存储验证 Web 状态流的终态。"""

import asyncio
import errno
import json
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

pytest.importorskip('fastapi')
from fastapi import FastAPI

from agent_core import config
from agent_core.core import sessions
from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app


WAIT = 5
TOKEN = 'sse-local-test'
HEADERS = {'Authorization': f'Bearer {TOKEN}'}


@asynccontextmanager
async def state_stream(app: FastAPI) -> AsyncIterator[asyncio.Queue[dict]]:
    """直接读取 ASGI 流式帧，避免测试 HTTP 客户端等待响应结束才返回。"""
    snapshots: asyncio.Queue[dict] = asyncio.Queue()
    incoming: asyncio.Queue[dict] = asyncio.Queue()
    incoming.put_nowait({'type': 'http.request', 'body': b'', 'more_body': False})
    scope = {
        'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1', 'method': 'GET', 'scheme': 'http', 'path': '/api/events',
        'query_string': b'', 'server': ('localhost', 80),
        'headers': [(b'host', b'localhost'), (b'authorization', HEADERS['Authorization'].encode())],
    }
    buffer = b''

    async def send(message: dict) -> None:
        nonlocal buffer
        if message['type'] == 'http.response.start':
            assert message['status'] == 200
            assert dict(message['headers'])[b'content-type'].startswith(b'text/event-stream')
        elif message['type'] == 'http.response.body':
            buffer += message.get('body', b'')
            while b'\n\n' in buffer:
                frame, buffer = buffer.split(b'\n\n', 1)
                prefix = b'event: state\ndata: '
                if frame.startswith(prefix):
                    snapshots.put_nowait(json.loads(frame[len(prefix):]))

    connection = asyncio.create_task(app(scope, incoming.get, send))
    try:
        yield snapshots
    finally:
        incoming.put_nowait({'type': 'http.disconnect'})
        await asyncio.wait_for(connection, WAIT)


async def next_session(snapshots: asyncio.Queue[dict], identifier: str,
                       predicate: Callable[[dict, dict], bool], *, reason: str) -> dict:
    last = None
    try:
        async with asyncio.timeout(WAIT):
            while True:
                snapshot = await snapshots.get()
                last = next(row for row in snapshot['sessions'] if row['id'] == identifier)
                if predicate(snapshot, last):
                    return last
    except TimeoutError:
        state = None if last is None else (last['status'], last['busy'])
        pytest.fail(f'{reason}；本次等待期间最后收到的状态/忙碌标记：{state}', pytrace=False)


@pytest.mark.parametrize(('case', 'outcome'), [
    pytest.param('completed', 'idle', id='completed'),
    pytest.param('cancelled', 'cancelled', id='cancelled'),
    pytest.param('initialization-error', 'error', id='initialization-error'),
    pytest.param('model-timeout', 'error', id='model-timeout'),
    pytest.param('stop-during-save', 'idle', id='stop-during-save'),
    pytest.param('disk-full', 'idle', id='disk-full'),
])
def test_sse_turn_becomes_idle_only_after_terminal_save(
    case: str, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox_env: Path,
) -> None:
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 0)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    disk_full = case == 'disk-full'
    terminal_status = 'error' if disk_full else outcome

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        entered_model = asyncio.Event()
        release_model = asyncio.Event()
        writing = asyncio.Event()
        write_finished = asyncio.Event()
        release_write = threading.Event()
        scheduled_revision = None

        async def model(history: list[dict], **kwargs) -> dict:
            if case == 'initialization-error':
                pytest.fail('预算初始化失败后调用了模型')
            entered_model.set()
            await release_model.wait()
            if case == 'model-timeout':
                raise TimeoutError('controlled model timeout')
            prompt = next(row['content'] for row in reversed(history) if row['role'] == 'user')
            return {'role': 'assistant', 'content': f'回复 {prompt}'}

        if case == 'initialization-error':
            def fail_initialization(*args, **kwargs) -> None:
                raise RuntimeError('controlled initialization failure')

            monkeypatch.setattr(sessions, 'limits_scope', fail_initialization)

        app = create_app(store=store, token=TOKEN, model=model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://localhost', headers=HEADERS) as client:
                created = await client.post('/api/sessions', json={'permission_policy': 'readonly'})
                assert created.status_code == 201, created.text
                identifier = created.json()['id']
                record_path = store.directory(identifier) / 'session.json'
                save_async = store.save_async
                atomic_write = store.atomic_write

                def track_final_submission(record: dict) -> asyncio.Future[None]:
                    nonlocal scheduled_revision
                    if record['status'] == outcome and scheduled_revision is None:
                        scheduled_revision = app.state.updates.revision
                    return save_async(record)

                def hold_final_commit(path: Path, content: str) -> None:
                    if path == record_path and json.loads(content)['status'] == outcome and not release_write.is_set():
                        loop.call_soon_threadsafe(writing.set)
                        assert release_write.wait(WAIT * 2), '测试未释放终态写入'
                        try:
                            if disk_full:
                                raise OSError(errno.ENOSPC, 'controlled disk full')
                            atomic_write(path, content)
                        finally:
                            loop.call_soon_threadsafe(write_finished.set)
                    else:
                        atomic_write(path, content)

                monkeypatch.setattr(store, 'save_async', track_final_submission)
                monkeypatch.setattr(store, 'atomic_write', hold_final_commit)

                async with state_stream(app) as snapshots:
                    initial = await next_session(snapshots, identifier, lambda *_: True, reason='未收到初始状态')
                    assert initial['status'] == 'idle' and initial['busy'] is False
                    assert initial['permission_policy'] == 'readonly'
                    try:
                        response = await client.post(f'/api/sessions/{identifier}/messages', json={'text': 'first'})
                        assert response.status_code == 202, response.text
                        if case != 'initialization-error':
                            await asyncio.wait_for(entered_model.wait(), WAIT)
                            await next_session(snapshots, identifier, lambda snapshot, row:
                                row['busy'] and snapshot['runtime']['active_model_calls'] == 1,
                                reason='未收到正在运行模型的状态')
                            if outcome == 'cancelled':
                                response = await client.post(f'/api/sessions/{identifier}/stop', json={})
                                assert response.status_code == 200, response.text
                            else:
                                release_model.set()

                        await asyncio.wait_for(writing.wait(), WAIT)
                        assert scheduled_revision is not None
                        # 确认常驻连接已消费写盘请求后的通知，再提交磁盘索引。
                        writing_state = await next_session(snapshots, identifier, lambda snapshot, _:
                            snapshot['revision'] > scheduled_revision, reason='未收到终态提交期间的状态')
                        assert store.read_record(record_path)['status'] != outcome
                        assert writing_state['busy'] is True, '会话终态仍在写盘，状态流已报告空闲'
                        blocked = await client.post(f'/api/sessions/{identifier}/messages', json={'text': 'too early'})
                        assert blocked.status_code == 409, blocked.text
                        if case == 'stop-during-save':
                            stopped = await client.post(f'/api/sessions/{identifier}/stop', json={})
                            assert stopped.status_code == 200, stopped.text
                            assert stopped.json()['busy'] is True
                            assert stopped.json()['status'] != 'stopping'
                    finally:
                        release_write.set()

                    await asyncio.wait_for(write_finished.wait(), WAIT)
                    saved = store.read_record(record_path)
                    if disk_full:
                        assert saved['status'] != 'idle'
                    else:
                        assert saved['status'] == outcome
                    assert [message['content'] for message in saved['messages'] if message['role'] == 'user'] == ['first']

                    def is_terminal(snapshot: dict, row: dict) -> bool:
                        if disk_full and not row['busy']:
                            assert row['status'] != 'idle', '保存失败后状态流报告成功'
                        return (row['status'] == terminal_status and not row['busy']
                                and snapshot['runtime']['active_agents'] == 0)

                    terminal = await next_session(snapshots, identifier, is_terminal,
                        reason=f'终态写入已结束，常驻 SSE 未发布 {terminal_status} 空闲状态')
                    # 首次提交失败时读到的是旧索引，终态通知后再核对错误快照。
                    saved = store.read_record(record_path)
                    assert saved['status'] == terminal_status
                    assert terminal['total_messages'] == len(saved['messages'])
                    if terminal_status == 'error':
                        assert terminal['error'] and 'controlled ' not in terminal['error']
                    if disk_full:
                        assert saved['messages'][-1]['content'] == '回复 first'
                    if case == 'completed':
                        assert saved['messages'][-1]['content'] == '回复 first'
                        entered_model.clear()
                        release_model.clear()
                        response = await client.post(f'/api/sessions/{identifier}/messages', json={'text': 'second'})
                        assert response.status_code == 202, response.text
                        await asyncio.wait_for(entered_model.wait(), WAIT)
                        await next_session(snapshots, identifier, lambda snapshot, row:
                            row['busy'] and row['total_messages'] == 4 and snapshot['runtime']['active_model_calls'] == 1,
                            reason='未收到同一会话紧接下一轮的活跃状态')
                        release_model.set()
                        await next_session(snapshots, identifier, lambda _, row:
                            row['status'] == 'idle' and not row['busy'] and row['total_messages'] == 5,
                            reason='未收到同一会话紧接下一轮的终态')
                        next_saved = store.read_record(record_path)
                        assert next_saved['messages'][-1]['content'] == '回复 second'

    try:
        asyncio.run(scenario())
    finally:
        store.close()
