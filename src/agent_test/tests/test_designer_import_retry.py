"""受控离线设计结果与真实临时 Flow 文件：只重试导入，不重跑模型。"""

import asyncio
from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient

from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


TOKEN = '1234567890abcdef1234567890abcdef'
TEMPLATE = {'format': 'agent-flow', 'version': 1, 'title': '受控设计模板',
            'nodes': [{'id': 'a', 'title': 'A', 'prompt': '按需求执行'}], 'edges': []}


@pytest.fixture
def flow_service(tmp_path, sandbox_env):
    store = SessionStore(tmp_path / 'state', sandbox_env)
    service = FlowService(SessionManager(store))
    yield service
    store.close()


def test_import_request_is_idempotent_across_service_reload_without_template_mutation(flow_service):
    payload = deepcopy(TEMPLATE)
    first = flow_service.import_template(payload, request_id=TOKEN)
    second = flow_service.import_template(payload, request_id=TOKEN)
    restored = FlowService(flow_service.manager)
    third = restored.import_template(payload, request_id=TOKEN)
    assert first['id'] == second['id'] == third['id']
    assert len(list(flow_service.directory.glob('*.json'))) == 1
    assert len(restored.records) == 1
    assert payload == TEMPLATE
    portable = restored.export_template(third)
    assert 'request_id' not in json.dumps(portable)
    assert TOKEN not in json.dumps(portable)


def test_import_without_request_id_still_creates_distinct_flows(flow_service):
    first = flow_service.import_template(deepcopy(TEMPLATE))
    second = flow_service.import_template(deepcopy(TEMPLATE))
    assert first['id'] != second['id']
    assert len(list(flow_service.directory.glob('*.json'))) == 2


def test_import_request_conflict_rejects_changed_payload_and_preserves_first_file(flow_service):
    first = flow_service.import_template(deepcopy(TEMPLATE), request_id=TOKEN)
    original = (flow_service.directory / (first['id'] + '.json')).read_bytes()
    changed = {**deepcopy(TEMPLATE), 'title': '改变后的模板'}
    with pytest.raises(ValueError):
        FlowService(flow_service.manager).import_template(changed, request_id=TOKEN)
    assert (flow_service.directory / (first['id'] + '.json')).read_bytes() == original
    assert len(list(flow_service.directory.glob('*.json'))) == 1


def test_import_request_rejects_a_flow_edited_after_original_import(flow_service):
    first = flow_service.import_template(deepcopy(TEMPLATE), request_id=TOKEN)
    first['title'] = '用户后来修改的模板'
    flow_service.save(first)
    with pytest.raises(ValueError):
        flow_service.import_template(deepcopy(TEMPLATE), request_id=TOKEN)
    assert len(list(flow_service.directory.glob('*.json'))) == 1


def test_import_retry_does_not_claim_success_after_external_file_change(flow_service):
    first = flow_service.import_template(deepcopy(TEMPLATE), request_id=TOKEN)
    path = flow_service.directory / (first['id'] + '.json')
    path.write_text('{"controlled": "uncertain-external-file"}', encoding='utf-8')
    changed = path.read_bytes()
    with pytest.raises((ValueError, PermissionError)):
        flow_service.import_template(deepcopy(TEMPLATE), request_id=TOKEN)
    assert path.read_bytes() == changed
    assert len(list(flow_service.directory.glob('*.json'))) == 1


@pytest.mark.parametrize('invalid', ['', 'short', 'A' * 32, '../' + 'a' * 29, 123, {}, []])
def test_import_rejects_invalid_request_id_without_creating_a_flow(flow_service, invalid):
    with pytest.raises(ValueError):
        flow_service.import_template(deepcopy(TEMPLATE), request_id=invalid)
    assert not flow_service.records
    assert not list(flow_service.directory.glob('*.json'))


@pytest.mark.parametrize('published', [False, True])
def test_failed_import_publication_cannot_create_a_second_flow(
        flow_service, monkeypatch, published):
    original_write = flow_service._write
    attempted_ids = []

    def fail_once(value, **kwargs):
        attempted_ids.append(value['id'])
        if len(attempted_ids) == 1:
            if published:
                original_write(value, **kwargs)
            raise OSError('controlled-publication-failure')
        return original_write(value, **kwargs)

    monkeypatch.setattr(flow_service, '_write', fail_once)
    payload = deepcopy(TEMPLATE)
    with pytest.raises(OSError):
        flow_service.import_template(payload, request_id=TOKEN)
    try:
        recovered = flow_service.import_template(payload, request_id=TOKEN)
    except (OSError, ValueError, PermissionError):
        assert published
    else:
        assert recovered['id'] == attempted_ids[0]
    assert len(set(attempted_ids)) == 1
    assert len(list(flow_service.directory.glob('*.json'))) == 1
    assert payload == TEMPLATE


def test_import_api_repeated_request_returns_one_persisted_flow(flow_service):
    from agent_frontend.web.app import create_app

    app = create_app(store=flow_service.manager.store)
    with TestClient(app, base_url='http://localhost') as client:
        body = {'template': deepcopy(TEMPLATE), 'request_id': TOKEN}
        first = client.post('/api/flows/import', json=body)
        second = client.post('/api/flows/import', json=body)
        assert first.status_code == second.status_code == 201
        assert first.json()['id'] == second.json()['id']
        assert len(list(flow_service.directory.glob('*.json'))) == 1
        conflict = client.post('/api/flows/import', json={
            **body, 'template': {**deepcopy(TEMPLATE), 'title': '不同内容'}})
        assert conflict.status_code == 409
        assert len(list(flow_service.directory.glob('*.json'))) == 1
        assert body['template'] == TEMPLATE


@pytest.fixture
def designer_bridge(flow_service, monkeypatch):
    from agent_frontend.desktop.runtime import RuntimeBridge
    import agent_core.core.flow_designer as designer

    bridge = RuntimeBridge(flow_service.manager.store.root,
                           workspace_root=flow_service.manager.store.workspace_root)
    bridge._store = flow_service.manager.store
    bridge._manager = flow_service.manager
    bridge._flows = flow_service
    bridge._mutations = asyncio.Lock()
    calls, results = [], []
    controlled_result = {'reply': '已完成设计', 'template': deepcopy(TEMPLATE)}

    async def design(*args, **kwargs):
        calls.append((args, kwargs))
        return controlled_result

    monkeypatch.setattr(designer, 'design_flow', design)
    bridge.finished.connect(lambda action, payload: results.append((action, json.loads(payload))))
    return bridge, calls, results, controlled_result


def _design_arguments(request_id=TOKEN, **extra):
    return {'messages': [{'role': 'user', 'content': '设计一个流程'}],
            'request_id': request_id, **extra}


@pytest.mark.parametrize('failure_stage', ['write', 'audit'])
def test_bridge_retries_only_cached_import_after_write_or_audit_failure(
        designer_bridge, monkeypatch, failure_stage):
    bridge, calls, results, controlled_result = designer_bridge
    target = bridge._flows if failure_stage == 'write' else bridge
    attribute = '_write' if failure_stage == 'write' else '_audit'
    original = getattr(target, attribute)
    failures = []

    def fail_once(*args, **kwargs):
        failures.append(True)
        if len(failures) == 1:
            raise OSError('controlled-private-import-failure')
        return original(*args, **kwargs)

    monkeypatch.setattr(target, attribute, fail_once)

    async def run():
        bridge._schedule_command('flow_design', _design_arguments())
        await bridge._design_task
        error = results[-1][1]
        assert error['request_id'] == TOKEN
        assert error['error_stage'] == 'import'
        assert error['retry_import_request_id'] == TOKEN
        assert 'controlled-private' not in error['error']
        retry_id = 'abcdef1234567890abcdef1234567890'
        bridge._schedule_command('flow_design', _design_arguments(
            retry_id, retry_import_request_id=TOKEN))
        await bridge._design_task
        success = results[-1][1]
        assert success['request_id'] == retry_id
        assert 'error' not in success
        assert success['template'] == TEMPLATE

    asyncio.run(run())
    assert len(calls) == 1
    assert len(bridge._flows.records) == 1
    assert len(list(bridge._flows.directory.glob('*.json'))) == 1
    assert controlled_result == {'reply': '已完成设计', 'template': TEMPLATE}


@pytest.mark.parametrize('invalidate', ['flow_design_cancel', 'flow_select'])
def test_bridge_cancel_or_switch_invalidates_cached_import_without_restarting_model(
        designer_bridge, monkeypatch, invalidate):
    bridge, calls, results, controlled_result = designer_bridge
    selection = bridge._flows.create(title='切换到此流程') if invalidate == 'flow_select' else None
    original_ids = set(bridge._flows.records)

    def fail_write(*args, **kwargs):
        raise OSError('controlled-private-import-failure')

    monkeypatch.setattr(bridge._flows, '_write', fail_write)

    async def run():
        bridge._schedule_command('flow_design', _design_arguments())
        await bridge._design_task
        assert results[-1][1]['retry_import_request_id'] == TOKEN
        bridge._schedule_command(invalidate, {'flow_id': selection['id']} if selection else {})
        if bridge._commands:
            await asyncio.gather(*list(bridge._commands))
        bridge._schedule_command('flow_design', _design_arguments(
            'abcdef1234567890abcdef1234567890', retry_import_request_id=TOKEN))
        await bridge._design_task
        assert 'error' in results[-1][1]
        assert 'controlled-private' not in results[-1][1]['error']

    asyncio.run(run())
    assert len(calls) == 1
    assert set(bridge._flows.records) == original_ids
    assert len(list(bridge._flows.directory.glob('*.json'))) == len(original_ids)
    assert controlled_result == {'reply': '已完成设计', 'template': TEMPLATE}
