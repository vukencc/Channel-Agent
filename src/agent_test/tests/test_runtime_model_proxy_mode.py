"""离线代理模式配置回归；不访问系统代理、真实供应商或用户状态。"""

from dataclasses import FrozenInstanceError
import json

import pytest
from fastapi.testclient import TestClient

from agent_core import config
from agent_core.core.model_settings import model_profile
from agent_core.core.runtime_models import RuntimeModelRegistry
from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app


SECRET = 'controlled-private-api-key-marker'
FIELDS = {'name': '测试连接', 'model': 'controlled-model',
          'base_url': 'https://example.invalid/v1'}


def test_registry_defaults_to_environment_and_reads_legacy_without_rewriting(tmp_path):
    registry = RuntimeModelRegistry(tmp_path / 'state')
    public = registry.add(**FIELDS, api_key=SECRET)
    assert public['proxy_mode'] == 'environment'
    data = json.loads(registry.path.read_text(encoding='utf-8'))
    data['models'][public['id']].pop('proxy_mode', None)
    registry.path.write_text(json.dumps(data, indent=3), encoding='utf-8')
    original = registry.path.read_bytes()

    restored = RuntimeModelRegistry(registry.root)
    assert restored.list_public()[0]['proxy_mode'] == 'environment'
    assert restored.resolve(public['id']).proxy_mode == 'environment'
    assert restored.resolve(public['id']).endpoint()['proxy_mode'] == 'environment'
    assert restored.path.read_bytes() == original
    assert SECRET not in json.dumps(restored.list_public())


@pytest.mark.parametrize('mode', ['environment', 'windows_system'])
def test_proxy_mode_persists_and_snapshot_is_immutable_routing_metadata(tmp_path, mode):
    registry = RuntimeModelRegistry(tmp_path / 'state')
    public = registry.add(**FIELDS, api_key=SECRET, proxy_mode=mode,
                          parameters={'temperature': .4})
    snapshot = registry.resolve(public['id'])
    assert public['proxy_mode'] == snapshot.proxy_mode == mode
    assert snapshot.endpoint()['proxy_mode'] == mode
    assert snapshot.options() == {'temperature': .4}
    assert SECRET not in repr(snapshot)
    assert SECRET not in json.dumps(public)
    with pytest.raises(FrozenInstanceError):
        snapshot.proxy_mode = 'environment'

    endpoint = snapshot.endpoint()
    endpoint['proxy_mode'] = 'invalid'
    assert snapshot.endpoint()['proxy_mode'] == mode
    registry.update(public['id'], **{**FIELDS, 'name': '更新连接'})
    assert registry.resolve(public['id']).proxy_mode == mode
    assert RuntimeModelRegistry(registry.root).list_public()[0]['proxy_mode'] == mode
    new_mode = 'environment' if mode == 'windows_system' else 'windows_system'
    changed = registry.update(public['id'], **FIELDS, proxy_mode=new_mode)
    assert changed['proxy_mode'] == new_mode
    assert snapshot.proxy_mode == mode
    assert registry.resolve(public['id']).api_key == SECRET


@pytest.mark.parametrize('invalid', ['', 'auto', 'system', None, True, {}, []])
def test_invalid_proxy_modes_fail_without_mutating_existing_private_file(tmp_path, invalid):
    registry = RuntimeModelRegistry(tmp_path / 'state')
    public = registry.add(**FIELDS, api_key=SECRET)
    original = registry.path.read_bytes()
    with pytest.raises(ValueError):
        registry.add(**FIELDS, api_key=SECRET, proxy_mode=invalid)
    with pytest.raises(ValueError):
        registry.update(public['id'], **FIELDS, proxy_mode=invalid)
    assert registry.path.read_bytes() == original
    assert registry.resolve(public['id']).proxy_mode == 'environment'


def test_environment_profile_public_metadata_uses_environment_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_PROFILES', {'configured': {'model': 'controlled'}})
    registry = RuntimeModelRegistry(tmp_path / 'state')
    row = registry.list_public()[0]
    assert row['read_only'] is True
    assert row['proxy_mode'] == 'environment'
    assert registry.resolve('configured') is None


def test_proxy_mode_is_not_forwarded_as_provider_options(tmp_path):
    from agent_core.core.llm import model_options

    registry = RuntimeModelRegistry(tmp_path / 'state')
    public = registry.add(**FIELDS, proxy_mode='windows_system',
                          parameters={'temperature': .5})
    snapshot = registry.resolve(public['id'])
    with model_profile(public['id'], snapshot):
        assert model_options() == {'temperature': .5}
    assert snapshot.endpoint()['proxy_mode'] == 'windows_system'


def test_models_api_creates_updates_and_preserves_omitted_proxy_mode(
        tmp_path, sandbox_env):
    store = SessionStore(tmp_path / 'state', sandbox_env)
    app = create_app(store=store)
    try:
        with TestClient(app, base_url='http://localhost') as client:
            default = client.post('/api/models', json=FIELDS)
            assert default.status_code == 201
            assert default.json()['proxy_mode'] == 'environment'
            created = client.post('/api/models', json={**FIELDS, 'api_key': SECRET,
                                                       'proxy_mode': 'windows_system'})
            assert created.status_code == 201
            public = created.json()
            assert public['proxy_mode'] == 'windows_system'
            path = '/api/models/' + public['id']
            updated = client.put(path, json={**FIELDS, 'name': '改名', 'api_key': ''})
            assert updated.status_code == 200
            assert updated.json()['proxy_mode'] == 'windows_system'
            listed = client.get('/api/models')
            row = next(row for row in listed.json()['models'] if row['id'] == public['id'])
            assert row['proxy_mode'] == 'windows_system'
            assert row['has_api_key'] is True
            for response in (created, updated, listed):
                assert SECRET not in response.text
            invalid = client.put(path, json={**FIELDS, 'proxy_mode': 'auto'})
            assert invalid.status_code == 422
            assert app.state.manager.model_registry.resolve(public['id']).proxy_mode == 'windows_system'
            changed = client.put(path, json={**FIELDS, 'proxy_mode': 'environment'})
            assert changed.status_code == 200
            assert changed.json()['proxy_mode'] == 'environment'
    finally:
        store.close()


def test_portable_flow_keeps_profile_reference_without_proxy_configuration(
        tmp_path, sandbox_env):
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import SessionManager

    store = SessionStore(tmp_path / 'state', sandbox_env)
    try:
        manager = SessionManager(store)
        public = manager.model_registry.add(**FIELDS, api_key=SECRET,
                                            proxy_mode='windows_system')
        service = FlowService(manager)
        flow = service.create(title='便携模板')
        flow['model_profile'] = public['id']
        flow['nodes'] = [{'id': 'a', 'title': 'A', 'prompt': '执行任务',
                          'model_profile': public['id']}]
        payload = service.export_template(service.save(flow))
        assert payload['model_profile'] == payload['nodes'][0]['model_profile'] == public['id']
        serialized = json.dumps(payload)
        for private_field in ('proxy_mode', 'windows_system', 'base_url', 'api_key', SECRET):
            assert private_field not in serialized
        imported = service.import_template(payload)
        assert imported['model_profile'] == public['id']
        before = set(service.records)
        for target in (payload, payload['nodes'][0]):
            target['proxy_mode'] = 'windows_system'
            with pytest.raises(ValueError):
                service.import_template(payload)
            del target['proxy_mode']
        assert set(service.records) == before
    finally:
        store.close()


@pytest.mark.parametrize('mode', ['environment', 'windows_system'])
def test_qt_model_dialog_preserves_proxy_mode_and_never_reads_back_key(
        tmp_path, monkeypatch, mode):
    """受控 Qt offscreen 界面：真实保存按钮提交到临时私有注册表。"""
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox
    from agent_frontend.desktop.window import ModelDialog

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    app = QApplication.instance() or QApplication([])
    registry = RuntimeModelRegistry(tmp_path / 'state')
    public = registry.add(**FIELDS, api_key=SECRET, proxy_mode=mode)
    default_dialog = ModelDialog()
    editor = ModelDialog(model=public)
    try:
        assert default_dialog.proxy_mode.currentData() == 'environment'
        assert default_dialog.api_key.text() == ''
        editor.show()
        app.processEvents()
        assert editor.proxy_mode.currentData() == mode
        if mode == 'windows_system':
            assert editor.proxy_mode.isVisible()
        assert editor.api_key.text() == ''
        assert 'api_key' not in public
        editor.name.setText('仅修改连接名称')
        buttons = editor.findChild(QDialogButtonBox)
        QTest.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Save),
                         Qt.MouseButton.LeftButton)
        app.processEvents()
        assert editor.result() == QDialog.DialogCode.Accepted
        payload = editor.result_payload
        assert payload['name'] == '仅修改连接名称'
        assert payload['proxy_mode'] == mode
        assert payload['api_key'] == editor.api_key.text() == ''
        saved = registry.update(public['id'], **payload)
        assert saved['proxy_mode'] == mode
        assert saved['has_api_key'] is True
        assert SECRET not in json.dumps(saved)
        assert registry.resolve(public['id']).api_key == SECRET
        assert RuntimeModelRegistry(registry.root).resolve(public['id']).proxy_mode == mode
    finally:
        default_dialog.close()
        editor.close()
        default_dialog.deleteLater()
        editor.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
