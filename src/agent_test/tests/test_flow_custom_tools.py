import pytest

from agent_core import config
from agent_core.core.flows import FlowService, Node
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import model_tools, selected_schemas
from agent_core.core.llm import TOOL_SCHEMAS


def spec(code="def main(arguments):\n    return 'one'\n"):
    return {'name': 'custom_example', 'description': 'Example',
            'parameters': {'type': 'object', 'properties': {}}, 'code': code}


@pytest.fixture
def manager(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield SessionManager(store)
    store.close()


def test_node_accepts_custom_tools():
    node = Node.model_validate({'id': 'one', 'title': 'One', 'prompt': 'Run', 'custom_tools': [spec()]})
    assert node.custom_tools[0].name == 'custom_example'


def test_round_trip_custom_definitions_and_reject_deployment(manager, monkeypatch):
    flow = FlowService(manager)
    definition = flow.create()
    definition['nodes'] = [{'id': 'a', 'title': 'A', 'prompt': 'Run',
                            'tool_names': ['custom_example'], 'custom_tools': [spec()]}]
    saved = flow.save(definition)
    payload = flow.export_template(saved)
    assert payload['nodes'][0]['custom_tools'][0]['code'] == spec()['code']
    assert flow.import_template(payload)['nodes'][0]['custom_tools'] == saved['nodes'][0]['custom_tools']
    monkeypatch.setattr(config, 'MODEL_TOOL_NAMES', ['read_file', 'list_files'])
    with pytest.raises(ValueError, match='run_command'):
        flow.import_template(payload)


def test_scoped_schemas_and_removal(manager):
    first = manager.create('One', 'One')
    second = manager.create('Two', 'Two')
    first.record['flow_id'] = 'flow'
    second.record['flow_id'] = 'flow'
    manager.set_custom_tools(first, [spec()])
    manager.set_custom_tools(second, [spec("def main(arguments):\n    return 'two'\n")])
    manager.set_tool_names(first, ['custom_example'])
    assert first.record['custom_tools'] != second.record['custom_tools']
    schemas = manager._session_schemas(first)
    with model_tools(manager._effective_session_tools(first), schemas):
        assert [row['function']['name'] for row in selected_schemas(TOOL_SCHEMAS)] == ['custom_example']
    assert 'custom_example' not in [row['function']['name'] for row in selected_schemas(TOOL_SCHEMAS)]
    manager.set_custom_tools(first, [])
    assert not manager._session_custom_registry(first)
    assert manager._effective_session_tools(first) == []
    assert 'custom_example' in manager._session_custom_registry(second)


def test_child_inherits_only_exact_parent_definitions(manager):
    parent = manager.create('Parent', 'Task')
    parent.record['flow_id'] = 'flow'
    manager.set_custom_tools(parent, [spec()])
    child = manager.create('Child', 'Task', parent=parent)
    child.record.update(delegated_from=parent.id, custom_tools=parent.record['custom_tools'].copy(),
                        inherited_custom_tools=parent.record['custom_tools'].copy(),
                        tool_names=['custom_example'], inherited_tool_names=['custom_example'])
    assert set(manager._session_custom_registry(child)) == {'custom_example'}
    with pytest.raises(ValueError, match='根会话'):
        manager.set_custom_tools(child, [spec()])
    manager.set_custom_tools(parent, [spec("def main(arguments):\n    return 'changed'\n")])
    assert manager._session_custom_registry(child) == {}
    assert manager._effective_session_tools(child) == []
