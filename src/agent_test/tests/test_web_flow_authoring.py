"""Web Flow 创作接口契约，使用隔离状态目录。"""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app


def test_flow_authoring_catalog_and_portable_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "workspace")
    try:
        with TestClient(create_app(store=store), base_url="http://localhost") as client:
            catalog = client.get("/api/flow-tools")
            assert catalog.status_code == 200
            assert catalog.json()["required_tool_names"]
            original = client.post("/api/flows", json={"title": "草稿"}).json()
            original["nodes"] = [{"id": "node", "title": "节点", "prompt": "说明"}]
            exported = client.post("/api/flows/export", json={"definition": original})
            assert exported.status_code == 200, exported.text
            portable = exported.json()
            assert portable["format"] == "agent-flow"
            assert "id" not in portable and "workspace_path" not in portable
            imported = client.post("/api/flows/import", json={"template": portable})
            assert imported.status_code == 201, imported.text
            assert imported.json()["id"] != original["id"]
            assert imported.json().get("project_id") is None
            assert imported.json()["nodes"][0]["id"] == "node"
    finally:
        store.close()


def test_designer_chat_is_isolated_and_does_not_save(tmp_path, monkeypatch):
    from agent_core.core import flow_designer

    calls = []

    async def controlled_designer(manager, messages, *, model_profile, reasoning_effort):
        calls.append((messages, model_profile, reasoning_effort))
        return {'reply': '请补充细节', 'template': None}

    monkeypatch.setattr(flow_designer, 'design_flow', controlled_designer)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            before = client.get('/api/flows').json()['flows']
            response = client.post('/api/flow-designer/chat', json={
                'messages': [{'role': 'user', 'content': '设计流程'}],
                'reasoning_effort': 'high'})
            assert response.status_code == 200, response.text
            assert response.json() == {'reply': '请补充细节', 'template': None}
            assert calls == [([{'role': 'user', 'content': '设计流程'}], None, 'high')]
            assert client.get('/api/flows').json()['flows'] == before
            invalid = client.post('/api/flow-designer/chat', json={
                'messages': [{'role': 'system', 'content': '敏感内容'}]})
            assert invalid.status_code == 422
            assert '敏感内容' not in invalid.text
    finally:
        store.close()


def test_import_invalid_template_does_not_echo_input_or_create_flow(tmp_path):
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            response = client.post('/api/flows/import', json={'template': {'secret': '敏感内容'}})
            assert response.status_code == 422
            assert '敏感内容' not in response.text
            assert client.get('/api/flows').json()['flows'] == []
    finally:
        store.close()


def test_designer_wait_does_not_block_other_mutations(tmp_path, monkeypatch):
    from agent_core.core import flow_designer

    started, release = threading.Event(), threading.Event()

    async def controlled_designer(manager, messages, *, model_profile, reasoning_effort):
        started.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return {'reply': '完成', 'template': None}

    monkeypatch.setattr(flow_designer, 'design_flow', controlled_designer)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            payload = {'messages': [{'role': 'user', 'content': '设计'}]}
            result = []
            thread = threading.Thread(target=lambda: result.append(client.post('/api/flow-designer/chat', json=payload)))
            thread.start()
            try:
                assert started.wait(3)
                assert client.post('/api/flow-designer/chat', json=payload).status_code == 409
                assert client.post('/api/flows', json={'title': '并发模板'}).status_code == 201
            finally:
                release.set()
                thread.join(3)
            assert not thread.is_alive()
            assert result[0].status_code == 200
    finally:
        store.close()


def test_designer_explicit_cancel_is_scoped_and_can_precede_start(tmp_path, monkeypatch):
    from agent_core.core import flow_designer

    started, cancelled, release = threading.Event(), threading.Event(), threading.Event()

    async def controlled_designer(manager, messages, *, model_profile, reasoning_effort):
        started.set()
        try:
            while not release.is_set():
                await asyncio.sleep(.01)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {'reply': '完成', 'template': None}

    monkeypatch.setattr(flow_designer, 'design_flow', controlled_designer)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store), base_url='http://localhost', raise_server_exceptions=False) as client:
            first, current = 'a' * 32, 'b' * 32
            assert client.post('/api/flow-designer/cancel', json={'request_id': first}).status_code == 200
            payload = {'messages': [{'role': 'user', 'content': '设计'}], 'request_id': first}
            assert client.post('/api/flow-designer/chat', json=payload).status_code == 409
            assert not started.is_set()
            payload['request_id'] = current
            thread = threading.Thread(target=lambda: client.post('/api/flow-designer/chat', json=payload))
            thread.start()
            try:
                assert started.wait(3)
                assert client.post('/api/flow-designer/cancel', json={'request_id': first}).status_code == 200
                assert not cancelled.wait(.1), '旧 ID 不得停止当前请求'
                assert client.post('/api/flow-designer/cancel', json={'request_id': current}).status_code == 200
                assert cancelled.wait(3), '停止必须实际取消设计任务'
            finally:
                release.set()
                thread.join(3)
            assert not thread.is_alive()
    finally:
        store.close()


def test_cancelled_designer_chat_returns_499_response(tmp_path, monkeypatch):
    from agent_core.core import flow_designer

    started = threading.Event()

    async def controlled_designer(manager, messages, *, model_profile, reasoning_effort):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(flow_designer, 'design_flow', controlled_designer)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    responses, exceptions = [], []
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            request_id = 'c' * 32
            def chat():
                try:
                    responses.append(client.post('/api/flow-designer/chat', json={
                        'request_id': request_id, 'messages': [{'role': 'user', 'content': '设计'}]}))
                except BaseException as exc:
                    exceptions.append(exc)
            thread = threading.Thread(target=chat)
            thread.start()
            try:
                assert started.wait(3)
                assert client.post('/api/flow-designer/cancel', json={'request_id': request_id}).status_code == 200
            finally:
                thread.join(3)
            assert not thread.is_alive()
            assert not exceptions, exceptions
            assert responses[0].status_code == 499
            assert responses[0].json()['detail'] == '设计请求已取消'
    finally:
        store.close()


def test_external_request_cancellation_propagates_and_drains_designer(tmp_path, monkeypatch):
    from agent_core.core import flow_designer
    from agent_frontend.web.app import DesignerChat

    started, cancelled = threading.Event(), threading.Event()

    async def controlled_designer(manager, messages, *, model_profile, reasoning_effort):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(flow_designer, 'design_flow', controlled_designer)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    app = create_app(store=store)
    try:
        with TestClient(app, base_url='http://localhost') as client:
            endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', None) == '/api/flow-designer/chat')
            async def disconnected():
                return False
            async def exercise():
                request = SimpleNamespace(is_disconnected=disconnected)
                arguments = DesignerChat(messages=[{'role': 'user', 'content': '设计'}])
                http_task = asyncio.create_task(endpoint(request, arguments))
                while not started.is_set():
                    await asyncio.sleep(.01)
                http_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await http_task
            client.portal.call(exercise)
            assert cancelled.is_set()
    finally:
        store.close()
