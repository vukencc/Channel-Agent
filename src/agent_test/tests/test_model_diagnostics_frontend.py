import asyncio
import json
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_core.core.storage import SessionStore
from agent_core.core.budgets import BudgetExceeded
from agent_frontend.desktop.runtime import RuntimeBridge
from agent_frontend.web.app import create_app


def test_web_model_connection_endpoint(tmp_path):
    async def model(*args, **kwargs):
        return {'content': 'private response'}
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store, model=model), base_url='http://localhost') as client:
            added = client.post('/api/models', json={'name': 'test', 'model': 'test-model',
                'base_url': 'https://example.invalid/v1', 'api_key': 'private-secret'}).json()
            before = store.root.joinpath('.runtime-models.json').read_bytes()
            sessions_before = set(client.app.state.manager.sessions)
            flows_before = set(client.app.state.flows.records)
            result = client.post('/api/models/' + added['id'] + '/test-connection', json={'request_id': 'first'})
            assert result.status_code == 200 and result.json()['ok']
            assert 'private' not in result.text
            assert before == store.root.joinpath('.runtime-models.json').read_bytes()
            assert set(client.app.state.manager.sessions) == sessions_before
            assert set(client.app.state.flows.records) == flows_before
            assert client.post('/api/models/missing/test-connection', json={'request_id': 'missing'}).status_code == 404
            assert client.post('/api/models/' + added['id'] + '/test-connection', json={'request_id': 'cross'}, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    finally:
        store.close()


def test_desktop_connection_correlated(tmp_path):
    async def run():
        async def model(*args, **kwargs):
            return {'content': 'private response'}
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        try:
            from agent_core.core.sessions import SessionManager
            bridge = RuntimeBridge(tmp_path, model=model)
            bridge._manager = SessionManager(store, model=model)
            bridge._mutations = asyncio.Lock()
            added = bridge._manager.model_registry.add(name='test', model='test-model', base_url='https://example.invalid/v1', api_key='private-secret')
            results = []
            bridge.finished.connect(lambda action, payload: results.append((action, json.loads(payload))))
            bridge._schedule_command('model_test_connection', {'model_id': added['id'], 'request_id': 'nonce-1'})
            await asyncio.gather(*list(bridge._commands))
            assert results[-1][0] == 'model_test_connection'
            assert results[-1][1]['request_id'] == 'nonce-1' and results[-1][1]['ok']
            assert 'private' not in json.dumps(results)
            await bridge._manager.shutdown()
        finally:
            store.close()
    asyncio.run(run())


def test_web_busy_does_not_hold_mutation_lock_and_budget_is_safe(tmp_path):
    started, release = threading.Event(), threading.Event()
    async def model(*args, **kwargs):
        started.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        raise BudgetExceeded('private-secret')
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store, model=model, token='token'), base_url='http://localhost', headers={'Authorization': 'Bearer token'}) as client:
            added = client.post('/api/models', json={'name': 'test', 'model': 'test-model', 'base_url': 'https://example.invalid/v1', 'api_key': 'private-secret'}).json()
            path = '/api/models/' + added['id'] + '/test-connection'
            output = []
            worker = threading.Thread(target=lambda: output.append(client.post(path, json={'request_id': 'first'})))
            worker.start()
            assert started.wait(5)
            assert client.post(path, json={'request_id': 'second'}).status_code == 409
            assert client.post('/api/models', json={'name': 'second', 'model': 'm', 'base_url': 'https://example.invalid/v1'}).status_code == 201
            assert client.post(path, json={'unknown': 'private-secret'}).status_code == 422
            release.set()
            worker.join(5)
            assert not worker.is_alive()
            assert output[0].json()['error_category'] == 'budget'
            assert 'private-secret' not in output[0].text
    finally:
        release.set()
        store.close()


def test_desktop_duplicate_cancel_and_invalid_nonce(tmp_path):
    async def run():
        started = asyncio.Event()
        async def model(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        try:
            from agent_core.core.sessions import SessionManager
            bridge = RuntimeBridge(tmp_path, model=model)
            bridge._manager = SessionManager(store, model=model)
            bridge._mutations = asyncio.Lock()
            identifier = bridge._manager.model_registry.add(name='test', model='m', base_url='https://example.invalid/v1')['id']
            results = []
            bridge.finished.connect(lambda action, payload: results.append((action, json.loads(payload))))
            bridge._schedule_command('model_test_connection', {'model_id': identifier, 'request_id': 'first'})
            await asyncio.wait_for(started.wait(), 2)
            bridge._schedule_command('model_test_connection', {'model_id': identifier, 'request_id': 'second'})
            bridge._schedule_command('model_test_connection', {'model_id': identifier, 'request_id': 'bad secret'})
            bridge._schedule_command('model_test_connection', {'model_id': 'missing', 'request_id': 'missing-id'})
            await asyncio.sleep(.05)
            assert any(x[1]['request_id'] == 'second' and x[1]['error_category'] == 'busy' for x in results)
            assert any(x[1]['request_id'] is None and x[1]['error_category'] == 'configuration' for x in results)
            assert any(x[1]['request_id'] == 'missing-id' and x[1]['model_id'] == 'missing' and x[1]['error_category'] == 'configuration' for x in results)
            bridge._schedule_command('model_test_cancel', {'request_id': 'unknown'})
            await asyncio.sleep(.01)
            assert any(x[0] == 'model_test_cancel' and x[1]['request_id'] == 'unknown' and x[1]['error_category'] == 'configuration' for x in results)
            assert bridge._model_test_task is not None
            bridge._schedule_command('model_test_cancel', {'request_id': 'first'})
            await asyncio.wait_for(asyncio.gather(*list(bridge._commands)), 2)
            assert any(x[0] == 'model_test_connection' and x[1]['request_id'] == 'first' and x[1]['error_category'] == 'cancelled' for x in results)
            assert bridge._model_test_task is None
            bridge._schedule_command('model_test_connection', {'model_id': identifier, 'request_id': 'late'})
            bridge._closing = True
            await asyncio.wait_for(asyncio.gather(*list(bridge._commands)), 2)
            assert any(x[1]['request_id'] == 'late' and x[1]['error_category'] == 'cancelled' for x in results)
            assert 'first' not in store.root.joinpath('audit.jsonl').read_text()
            await bridge._manager.shutdown()
        finally:
            store.close()
    asyncio.run(run())


def test_web_explicit_cancel_running_and_early(tmp_path):
    started, drained = threading.Event(), threading.Event()
    calls = []
    async def model(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            return {'content': 'ok'}
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store, model=model), base_url='http://localhost') as client:
            def add(name):
                return client.post('/api/models', json={'name': name, 'model': 'm', 'base_url': 'https://example.invalid/v1'}).json()['id']
            identifier, other = add('one'), add('two')
            path = '/api/models/' + identifier + '/test-connection'
            other_path = '/api/models/' + other + '/test-connection'
            assert client.post(path + '/cancel', json={'request_id': 'early'}).status_code == 200
            assert client.post(path, json={'request_id': 'early'}).status_code == 409
            assert not calls
            output = []
            worker = threading.Thread(target=lambda: output.append(client.post(path, json={'request_id': 'active'})))
            worker.start()
            assert started.wait(5)
            assert client.post(other_path + '/cancel', json={'request_id': 'active'}).status_code == 200
            assert not drained.is_set()
            assert client.post(path + '/cancel', json={'request_id': 'active'}).status_code == 200
            assert drained.wait(2)
            worker.join(5)
            assert not worker.is_alive() and output[0].status_code == 499
            assert client.post(path, json={'request_id': 'active'}).status_code == 409
            assert client.post(path, json={'request_id': 'fresh'}).json()['ok']
            assert client.post(path + '/cancel', json={'request_id': 'bad secret'}).status_code == 422
            assert 'active' not in store.root.joinpath('audit.jsonl').read_text()
    finally:
        store.close()


def test_web_asgi_disconnect_then_explicit_cancel_drains(tmp_path):
    async def run():
        started, drained = asyncio.Event(), asyncio.Event()
        async def model(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        app = create_app(store=store, model=model)
        try:
            async with app.router.lifespan_context(app):
                identifier = app.state.manager.model_registry.add(name='test', model='m', base_url='https://example.invalid/v1')['id']
                path = '/api/models/' + identifier + '/test-connection'
                def request(path):
                    queue = asyncio.Queue()
                    body = b'{"request_id":"asgi"}'
                    queue.put_nowait({'type': 'http.request', 'body': body, 'more_body': False})
                    sent = []
                    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                             'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                             'query_string': b'', 'headers': [(b'host', b'localhost'), (b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())],
                             'server': ('localhost', 80), 'client': ('127.0.0.1', 12345), 'root_path': ''}
                    async def send(message):
                        sent.append(message)
                    return queue, sent, asyncio.create_task(app(scope, queue.get, send))
                queue, _, pending = request(path)
                await asyncio.wait_for(started.wait(), 2)
                queue.put_nowait({'type': 'http.disconnect'})
                _, messages, cancel = request(path + '/cancel')
                await asyncio.wait_for(drained.wait(), 2)
                await asyncio.wait_for(asyncio.gather(pending, cancel), 2)
                assert any(message['type'] == 'http.response.start' and message['status'] == 200 for message in messages)
                assert app.state.model_test_task is None
        finally:
            store.close()
    asyncio.run(run())


def test_web_asgi_disconnect_alone_drains_and_allows_next_check(tmp_path):
    async def run():
        started, drained = asyncio.Event(), asyncio.Event()
        calls = []
        async def model(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                return {'content': 'ok'}
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        app = create_app(store=store, model=model)
        try:
            async with app.router.lifespan_context(app):
                identifier = app.state.manager.model_registry.add(name='test', model='m', base_url='https://example.invalid/v1')['id']
                path = '/api/models/' + identifier + '/test-connection'
                queue = asyncio.Queue()
                body = b'{"request_id":"disconnect"}'
                queue.put_nowait({'type': 'http.request', 'body': body, 'more_body': False})
                scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST',
                         'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
                         'headers': [(b'host', b'localhost'), (b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())],
                         'server': ('localhost', 80), 'client': ('127.0.0.1', 12345), 'root_path': ''}
                async def send(message):
                    pass
                pending = asyncio.create_task(app(scope, queue.get, send))
                await asyncio.wait_for(started.wait(), 2)
                queue.put_nowait({'type': 'http.disconnect'})
                await asyncio.wait_for(drained.wait(), 2)
                await asyncio.wait_for(pending, 2)
                assert app.state.model_test_task is None
                assert not app.state.manager.slots.locked()
                next_queue = asyncio.Queue()
                next_queue.put_nowait({'type': 'http.request', 'body': body, 'more_body': False})
                messages = []
                async def next_send(message):
                    messages.append(message)
                await asyncio.wait_for(app(scope, next_queue.get, next_send), 2)
                assert any(message['type'] == 'http.response.start' and message['status'] == 200 for message in messages)
        finally:
            store.close()
    asyncio.run(run())


def test_web_cancel_after_model_deleted_drains_and_unlocks_mutations(tmp_path):
    started, drained = threading.Event(), threading.Event()
    async def model(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store, model=model), base_url='http://localhost') as client:
            identifier = client.post('/api/models', json={'name': 'test', 'model': 'm', 'base_url': 'https://example.invalid/v1'}).json()['id']
            path = '/api/models/' + identifier + '/test-connection'
            check_results, cancel_results = [], []
            check = threading.Thread(target=lambda: check_results.append(client.post(path, json={'request_id': 'deleted'})), daemon=True)
            check.start()
            assert started.wait(5)
            assert client.delete('/api/models/' + identifier, headers={'content-length': '0'}).status_code == 200
            cancel = threading.Thread(target=lambda: cancel_results.append(client.post(path + '/cancel', json={'request_id': 'deleted'})), daemon=True)
            cancel.start()
            cancel.join(3)
            assert not cancel.is_alive()
            assert cancel_results[0].status_code == 200
            assert drained.wait(2)
            check.join(3)
            assert not check.is_alive() and check_results[0].status_code == 499
            assert client.post('/api/models', json={'name': 'after', 'model': 'm', 'base_url': 'https://example.invalid/v1'}).status_code == 201
    finally:
        store.close()


def test_desktop_cancel_while_waiting_for_mutation_lock(tmp_path):
    async def run():
        calls = []
        async def model(*args, **kwargs):
            calls.append(1)
            return {'content': 'ok'}
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        try:
            from agent_core.core.sessions import SessionManager
            bridge = RuntimeBridge(tmp_path, model=model)
            bridge._manager = SessionManager(store, model=model)
            bridge._mutations = asyncio.Lock()
            identifier = bridge._manager.model_registry.add(name='test', model='m', base_url='https://example.invalid/v1')['id']
            results = []
            bridge.finished.connect(lambda action, payload: results.append((action, json.loads(payload))))
            await bridge._mutations.acquire()
            bridge._schedule_command('model_test_connection', {'model_id': identifier, 'request_id': 'queued'})
            bridge._schedule_command('model_test_cancel', {'request_id': 'other'})
            await asyncio.sleep(.01)
            assert bridge._model_test_id == 'queued'
            assert any(action == 'model_test_cancel' and result['request_id'] == 'other' and result['error_category'] == 'configuration' for action, result in results)
            bridge._schedule_command('model_test_cancel', {'request_id': 'queued'})
            await asyncio.sleep(.02)
            bridge._mutations.release()
            await asyncio.wait_for(asyncio.gather(*list(bridge._commands)), 2)
            assert calls == []
            assert any(action == 'model_test_connection' and result['request_id'] == 'queued' and result['error_category'] == 'cancelled' for action, result in results)
            await bridge._manager.shutdown()
        finally:
            store.close()
    asyncio.run(run())
