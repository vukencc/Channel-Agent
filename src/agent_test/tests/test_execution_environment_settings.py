"""可信会话入口选择 Linux 本机执行，模型不能改变授权环境。"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_core import config
from agent_core.core.storage import SessionStore
from agent_core.tools import TOOL_REGISTRY
from agent_frontend.web.app import create_app


@pytest.fixture
def environment_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_EXECUTION_ENVIRONMENT', 'sandbox')
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    async def no_model(*args, **kwargs):
        raise AssertionError('配置验证不调用模型')
    try:
        with TestClient(create_app(store=store, model=no_model), base_url='http://localhost') as client:
            yield client
    finally:
        store.close()


def test_user_can_select_local_on_creation_and_restore_service_default(environment_client):
    client = environment_client
    response = client.post('/api/sessions', json={'title': '本机项目', 'execution_environment': 'local'})
    assert response.status_code == 201, response.text
    value = response.json()
    assert value['execution_environment'] == 'local'
    manager = client.app.state.manager
    session = manager.sessions[value['id']]
    assert session.record['execution_environment'] == 'local'
    saved = client.patch('/api/sessions/' + session.id, json={'execution_environment': None})
    assert saved.status_code == 200, saved.text
    assert saved.json()['execution_environment'] == 'sandbox'


def test_children_inherit_execution_environment_and_cannot_change_it(environment_client):
    client = environment_client
    parent_id = client.post('/api/sessions', json={'title': '父项目'}).json()['id']
    manager = client.app.state.manager
    parent = manager.sessions[parent_id]
    manager.set_execution_environment(parent, 'local')
    child = manager.create('子 Agent', parent=parent)
    assert manager.execution_environment(child) == 'local'
    response = client.patch('/api/sessions/' + child.id, json={'execution_environment': 'sandbox'})
    assert response.status_code == 403, response.text
    child.tool_workers.add('running')
    try:
        response = client.patch('/api/sessions/' + parent.id, json={'execution_environment': 'sandbox'})
        assert response.status_code == 409, response.text
        assert manager.execution_environment(parent) == 'local'
    finally:
        child.tool_workers.clear()
    manager.set_execution_environment(parent, 'sandbox')
    assert manager.execution_environment(child) == 'sandbox'


def test_model_session_schema_does_not_grant_environment_selection():
    with pytest.raises(ValidationError):
        TOOL_REGISTRY['create_session'].args_model.model_validate({'task': '工作', 'execution_environment': 'local'})


@pytest.mark.parametrize('default,selected', [('local', 'sandbox'), ('sandbox', 'local')])
def test_branch_keeps_effective_execution_environment(environment_client, monkeypatch, default, selected):
    manager = environment_client.app.state.manager
    monkeypatch.setattr(config, 'COMMAND_EXECUTION_ENVIRONMENT', default)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BRANCHES', True)
    source = manager.create('源会话', execution_environment=selected)
    branch = environment_client.portal.call(manager.fork_session, source)
    assert branch.record['execution_environment'] == selected
    assert manager.execution_environment(branch) == selected
