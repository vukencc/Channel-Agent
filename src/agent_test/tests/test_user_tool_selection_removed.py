"""工具能力由框架管理，界面和 HTTP 不接受用户工具清单。"""
from pathlib import Path

from fastapi.testclient import TestClient

from agent_core import config
from agent_core.core.storage import SessionStore
from agent_frontend.cli.commands import COMMANDS, help_text
from agent_frontend.web.app import create_app


def test_user_settings_reject_tool_selection_without_changing_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DEBUG', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            created = client.post('/api/sessions', json={'title': '自动工具'}).json()
            identifier = created['id']
            record = client.app.state.manager.sessions[identifier].record
            before = record.get('tool_names')
            for names in ([], ['read_file'], None):
                response = client.patch('/api/sessions/' + identifier, json={'tool_names': names})
                assert response.status_code == 422
                assert record.get('tool_names') == before
    finally:
        store.close()


def test_cli_no_longer_advertises_tool_selection():
    assert '/tools' not in {command.name for command in COMMANDS}
    assert '/tools' not in help_text()


def test_web_settings_have_no_tool_picker_or_manual_tool_payload():
    root = Path(__file__).resolve().parents[3]
    assets = root / 'src/agent_frontend/web/static'
    html = (assets / 'index.html').read_text(encoding='utf-8')
    javascript = (assets / 'app.js').read_text(encoding='utf-8')
    assert 'settings-tools' not in html
    assert 'settings-tools' not in javascript
    assert 'settingsAllTools' not in javascript
    assert 'tool_names:' not in javascript
