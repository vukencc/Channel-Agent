"""受控模型回调验收；Python 工具经真实隔离命令执行，不调用外部服务。"""
import asyncio
import copy
import json

import pytest

from agent_core import config
from agent_core.core import sessions
from agent_core.core.flow_designer import design_flow
from agent_core.core.flows import FlowService
from agent_core.core.llm import TOOL_SCHEMAS
from agent_core.core.instrumentation import NullObserver
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import selected_schemas


def definition(name, code, properties=None, required=None):
    return {'name': name, 'description': '离线合成反馈工具', 'code': code,
            'parameters': {'type': 'object', 'properties': properties or {},
                           'required': required or [], 'additionalProperties': False}}


def writer(name, filename):
    return definition(name,
        'import json\nfrom pathlib import Path\ndef main(arguments):\n'
        f'    path = Path({filename!r})\n'
        '    path.write_text(json.dumps(arguments), encoding="utf-8")\n'
        '    return {"stored": str(path), "feedback_id": arguments["feedback_id"]}\n',
        {'feedback_id': {'type': 'string', 'minLength': 1},
         'message': {'type': 'string', 'minLength': 1}}, ['feedback_id', 'message'])


def tool_call(name, arguments, identifier='controlled-call'):
    return {'id': identifier, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}


@pytest.fixture
def isolated_runtime(sandbox_env, tmp_path, monkeypatch):
    for name in ('RAG_ASSESS', 'MEMORY_AUTO_EXTRACT', 'ENABLE_AGENT_TASKS',
                 'ENABLE_STRUCTURED_CONTEXT', 'AUTO_CONTINUE_TASKS'):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'off')
    monkeypatch.setattr(config, 'COMMAND_EXECUTION_ENVIRONMENT', 'sandbox')
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    store = SessionStore(tmp_path / 'state', tmp_path / 'private')
    yield store, sandbox_env
    store.close()


def test_designed_feedback_flow_executes_and_round_trips(isolated_runtime, monkeypatch):
    store, workspace = isolated_runtime
    feedback = [{'id': 'synthetic-1', 'text': '使用问题'}, {'id': 'synthetic-2', 'text': '需要人工处理'}]
    (workspace / 'feedback.json').write_text(json.dumps(feedback), encoding='utf-8')
    tools = [definition('read_feedback',
        'import json\nfrom pathlib import Path\ndef main(arguments):\n'
        '    return json.loads(Path("feedback.json").read_text(encoding="utf-8"))\n'),
        writer('send_ai_reply', 'outbox.json'), writer('submit_human', 'human-queue.json')]
    template = {'format': 'agent-flow', 'version': 1, 'title': '合成反馈验收',
                'nodes': [{'id': 'support', 'title': '客服', 'prompt': '处理合成反馈',
                           'tool_names': [tool['name'] for tool in tools], 'custom_tools': tools}], 'edges': []}
    budget_schemas, provider_schemas, actual_results = [], [], []
    events = []

    class CaptureObserver(NullObserver):
        enabled = True

        def event(self, kind, *, session_id=None, **fields):
            events.append((kind, fields))
    original_prepare = sessions.prepare_model_history

    async def observed_prepare(*args, **kwargs):
        budget_schemas.append(copy.deepcopy(kwargs['schemas']))
        return await original_prepare(*args, **kwargs)

    monkeypatch.setattr(sessions, 'prepare_model_history', observed_prepare)
    steps = [tool_call('read_feedback', {}, 'read'),
             tool_call('send_ai_reply', {'feedback_id': 'synthetic-1', 'message': '使用说明'}, 'reply'),
             tool_call('submit_human', {'feedback_id': 'synthetic-2', 'message': '待人工核对'}, 'human')]

    async def controlled_model(history, *, session_id, emit):
        if session_id is None:
            assert selected_schemas(TOOL_SCHEMAS) == []
            return {'role': 'assistant', 'content': json.dumps({'reply': '生成未执行模板', 'template': template})}
        provider_schemas.append(copy.deepcopy(selected_schemas(TOOL_SCHEMAS)))
        assert provider_schemas[-1] == budget_schemas[-1]
        previous = [row for row in history if row['role'] == 'tool']
        if previous:
            actual_results.append(previous[-1])
            assert '[退出码] 0' in previous[-1]['content'], previous[-1]
        if len(previous) < len(steps):
            return {'role': 'assistant', 'content': None, 'tool_calls': [steps[len(previous)]]}
        return {'role': 'assistant', 'content': '已写入本地 outbox 和人工队列，未发送外部消息。'}

    async def scenario():
        manager = SessionManager(store, model=controlled_model, observer=CaptureObserver())
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        try:
            designed = await design_flow(manager, [{'role': 'user', 'content': '设计反馈分流工具'}])
            assert not (workspace / 'outbox.json').exists()
            saved = flows.import_template(designed['template'])
            portable = flows.export_template(saved)
            imported = flows.import_template(portable)
            assert imported['nodes'][0]['custom_tools'] == saved['nodes'][0]['custom_tools']
            project = projects.create('隔离项目', str(workspace), flow_id=imported['id'],
                                      execution_environment='sandbox', permission_policy='full_access')
            flows.start(project['flow_id'])
            await asyncio.wait_for(flows.tasks[project['flow_id']], 20)
            result = flows.get(project['flow_id'])['run']
            assert result['status'] == 'completed', result
            assert json.loads((workspace / 'outbox.json').read_text()) == {
                'feedback_id': 'synthetic-1', 'message': '使用说明'}
            assert json.loads((workspace / 'human-queue.json').read_text()) == {
                'feedback_id': 'synthetic-2', 'message': '待人工核对'}
            assert 'synthetic-1' in actual_results[0]['content']
            assert len(provider_schemas) == len(budget_schemas) == 4
            for event in ('tool.started', 'tool.completed'):
                assert [fields['tool_name'] for kind, fields in events if kind == event] == [
                    'read_feedback', 'send_ai_reply', 'submit_human']
            custom = {row['function']['name']: row for row in provider_schemas[0]}
            for spec in tools:
                assert custom[spec['name']]['function']['parameters'] == spec['parameters']
            monkeypatch.setattr(config, 'MODEL_TOOL_NAMES', ['read_file', 'list_files'])
            with pytest.raises(ValueError, match='run_command'):
                flows.import_template(portable)
        finally:
            await flows.shutdown()
            await manager.shutdown()

    asyncio.run(scenario())


def make_session(manager, workspace, spec, policy='full_access'):
    session = manager.create('受控工具验收', '执行真实工具', workspace_path=str(workspace),
                             execution_environment='sandbox')
    session.record.update(flow_id='a' * 32, permission_policy=policy)
    manager.set_custom_tools(session, [spec])
    manager.set_tool_names(session, [spec['name']])
    return session


def test_wrong_arguments_do_not_execute_and_same_names_remain_private(isolated_runtime):
    store, workspace = isolated_runtime

    async def scenario():
        manager = SessionManager(store)
        try:
            first = make_session(manager, workspace, writer('send_ai_reply', 'first.json'))
            second = make_session(manager, workspace, writer('send_ai_reply', 'second.json'))
            for bad in ({'feedback_id': 12, 'message': 'bad'}, {'feedback_id': 'x'},
                        {'feedback_id': 'x', 'message': 'bad', 'extra': True}):
                with pytest.raises(ValueError, match='schema'):
                    await manager._tool(first, tool_call('send_ai_reply', bad))
                assert not (workspace / 'first.json').exists()
            for session, filename in ((first, 'first.json'), (second, 'second.json')):
                result = await manager._tool(session, tool_call('send_ai_reply', {
                    'feedback_id': session.id, 'message': filename}))
                assert '[退出码] 0' in result, result
                assert json.loads((workspace / filename).read_text())['feedback_id'] == session.id
            assert json.loads((workspace / 'first.json').read_text())['message'] == 'first.json'
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['readonly', 'failure', 'timeout', 'cancel'])
def test_custom_runtime_failure_and_lifecycle(isolated_runtime, monkeypatch, mode):
    store, workspace = isolated_runtime
    code = ('from pathlib import Path\nimport time\ndef main(arguments):\n'
            '    Path("started").write_text("started")\n')
    if mode == 'failure':
        code += '    raise RuntimeError("controlled-code-failure")\n'
    elif mode in {'timeout', 'cancel'}:
        code += '    time.sleep(30)\n'
    code += '    Path("must-not-complete").write_text("completed")\n    return "done"\n'
    if mode == 'timeout':
        monkeypatch.setattr(config, 'COMMAND_TIMEOUT', .3)

    async def scenario():
        manager = SessionManager(store)
        try:
            session = make_session(manager, workspace, definition('lifecycle_probe', code),
                                   policy='readonly' if mode == 'readonly' else 'full_access')
            task = asyncio.create_task(manager._tool(session, tool_call('lifecycle_probe', {})))
            if mode == 'cancel':
                async with asyncio.timeout(8):
                    while not (workspace / 'started').exists():
                        if task.done():
                            pytest.fail(f'命令未启动：{task.result()}')
                        await asyncio.sleep(.02)
                session.cancelled.set()
            result = await asyncio.wait_for(task, 8)
            if mode == 'readonly':
                assert '拒绝' in result or '拦截' in result, result
                assert not (workspace / 'started').exists()
            elif mode == 'failure':
                assert 'controlled-code-failure' in result and '[退出码] 0' not in result, result
            elif mode == 'timeout':
                assert '超时' in result, result
            else:
                assert '取消' in result, result
            assert not (workspace / 'must-not-complete').exists()
            assert not session.tool_workers and not manager._active_tools
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
