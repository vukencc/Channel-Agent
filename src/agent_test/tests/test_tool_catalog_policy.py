"""模型目录简化不改变执行注册表或历史会话的能力边界。"""
import asyncio
import copy
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.session_service import open_session
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import bounded_schemas
from agent_core.tools import TOOL_REGISTRY, all_schemas
from agent_core.tools.agent_tasks import register_agent_tasks
from agent_core.tools.command_jobs import register_command_jobs


@pytest.fixture
def compatibility_schemas():
    previous = dict(TOOL_REGISTRY)
    register_agent_tasks()
    register_command_jobs()
    try:
        yield all_schemas()
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(previous)


def names(schemas):
    return [row['function']['name'] for row in schemas]


def test_automatic_catalog_removes_redundant_aliases_without_changing_registry(compatibility_schemas):
    original = copy.deepcopy(compatibility_schemas)
    catalog = bounded_schemas(compatibility_schemas, None)
    assert {'create_session', 'wait_agent_task', 'cancel_agent_task', 'inspect_command_job'} <= set(names(catalog))
    assert not {'delegate', 'task_status', 'job_status', 'job_logs'} & set(names(catalog))
    assert names(catalog) == [name for name in names(original) if name not in
                              {'delegate', 'task_status', 'job_status', 'job_logs'}]
    assert compatibility_schemas == original
    assert {'delegate', 'task_status', 'job_status', 'job_logs'} <= set(TOOL_REGISTRY)
    assert len(catalog) == len(original) - 4
    assert len(json.dumps(catalog, ensure_ascii=False)) < len(json.dumps(original, ensure_ascii=False))


@pytest.mark.parametrize('allowed', [
    ['delegate', 'task_status', 'job_status', 'job_logs'],
    ['job_status'], ['job_logs'], [],
])
def test_explicit_legacy_only_policy_is_not_widened(compatibility_schemas, allowed):
    assert set(names(bounded_schemas(compatibility_schemas, allowed))) == set(allowed)


def test_explicit_policy_with_canonical_capability_hides_only_replaced_aliases(compatibility_schemas):
    allowed = ['create_session', 'delegate', 'task_status', 'inspect_command_job', 'job_status']
    assert set(names(bounded_schemas(compatibility_schemas, allowed))) == {
        'create_session', 'task_status', 'inspect_command_job'}


def test_user_root_creation_does_not_depend_on_model_tool_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.delitem(TOOL_REGISTRY, 'create_session')
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def run():
        manager = SessionManager(store)
        try:
            root = open_session(manager, '可信界面', execution_environment='sandbox')
            await manager.flush()
            assert root.record['title'] == '可信界面'
            assert root.record['execution_environment'] == 'sandbox'
            rows = [json.loads(line) for line in (store.directory(root.id) / 'audit.jsonl').read_text().splitlines()]
            assert any(row['event'] == 'session_created' and row['source'] == 'user' for row in rows)
            before = set(manager.sessions)
            with pytest.raises(ValueError):
                open_session(manager, '')
            assert set(manager.sessions) == before
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_default_create_supports_delayed_child_and_owned_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *_: True)
        parent = manager.create()
        unrelated = manager.create()
        def call(name, **arguments):
            return {'id': name, 'function': {'name': name, 'arguments': json.dumps(arguments)}}
        try:
            created = json.loads(await manager._tool(parent, call('create_session',
                task='等待父任务取消', tool_names=[], delay_seconds=60)))
            assert manager.agent_tasks.status(parent.id, created['task_id'])['status'] == 'queued'
            with pytest.raises(PermissionError):
                await manager._tool(unrelated, call('cancel_agent_task', task_id=created['task_id']))
            assert manager.agent_tasks.status(parent.id, created['task_id'])['status'] == 'queued'
            result = await manager._tool(parent, call('cancel_agent_task', task_id=created['task_id']))
            assert '取消' in result
            await asyncio.gather(manager.agent_tasks.tasks[created['task_id']], return_exceptions=True)
            assert manager.agent_tasks.status(parent.id, created['task_id'])['status'] == 'cancelled'
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_cancellation_registered_when_legacy_agent_tasks_disabled():
    result = subprocess.run([sys.executable, '-c',
        'from agent_core.tools import TOOL_REGISTRY; '
        'assert "create_session" in TOOL_REGISTRY; '
        'assert "cancel_agent_task" in TOOL_REGISTRY; '
        'assert "delegate" not in TOOL_REGISTRY'],
        env={**os.environ, 'ENABLE_AGENT_TASKS': 'false', 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_legacy_registration_does_not_replace_shared_cancellation(compatibility_schemas):
    original = TOOL_REGISTRY['cancel_agent_task']
    register_agent_tasks()
    assert TOOL_REGISTRY['cancel_agent_task'] is original


@pytest.mark.parametrize('legacy_enabled', [False, True])
def test_registered_cancellation_accepts_legacy_tool_context_and_preserves_ownership(tmp_path,
                                                                                   monkeypatch,
                                                                                   legacy_enabled):
    from agent_core.core.agent_tasks import AgentTasks
    from agent_core.core.session_service import session_service_context
    from agent_core.tools.sandbox import ToolContext, tool_context
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', legacy_enabled)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    previous = dict(TOOL_REGISTRY)
    if legacy_enabled:
        register_agent_tasks()
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def run():
        manager = SessionManager(store)
        try:
            parent, unrelated = manager.create(), manager.create()
            if manager.agent_tasks is None:
                manager.agent_tasks = AgentTasks(manager, require_budget_config=False)
            identifier = await manager.agent_tasks.start(parent, '等待兼容宿主取消', [], delay_seconds=60)
            context = ToolContext(manager.workspace(parent), store.directory(parent.id) / 'audit.jsonl',
                lambda *_: False, parent.cancelled, session_id=parent.id,
                agent_tasks=manager.agent_tasks, event_loop=asyncio.get_running_loop())
            arguments = json.dumps({'task_id': identifier})
            tool = TOOL_REGISTRY['cancel_agent_task']
            # 已存在的服务上下文拒绝调用时，不得回退为旧宿主权限。
            with tool_context(context), session_service_context(manager, None, context.event_loop):
                with pytest.raises(PermissionError):
                    await asyncio.to_thread(tool.run, arguments)
            assert manager.agent_tasks.status(parent.id, identifier)['status'] == 'queued'
            context.session_id = unrelated.id
            with tool_context(context), pytest.raises(PermissionError):
                await asyncio.to_thread(tool.run, arguments)
            assert manager.agent_tasks.status(parent.id, identifier)['status'] == 'queued'
            context.session_id = parent.id
            with tool_context(context):
                result = await asyncio.to_thread(tool.run, arguments)
            assert '取消' in result
            await asyncio.gather(manager.agent_tasks.tasks[identifier], return_exceptions=True)
            assert manager.agent_tasks.status(parent.id, identifier)['status'] == 'cancelled'
            rows = [json.loads(line) for line in context.audit_path.read_text().splitlines()]
            assert any(row['event'] == 'agent_task_cancel_requested' and
                       row['session_id'] == parent.id and row['task_id'] == identifier for row in rows)
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(previous)


def test_root_creation_audit_uses_selected_workspace_identity_and_environment(tmp_path, monkeypatch):
    from agent_core.tools import sandbox
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    project = tmp_path / 'selected-project'
    project.mkdir()
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    observed = []
    original = sandbox.audit

    def capture(event, **fields):
        if event == 'session_created':
            observed.append(sandbox._context.get())
        original(event, **fields)

    monkeypatch.setattr(sandbox, 'audit', capture)

    async def run():
        manager = SessionManager(store)
        try:
            root = open_session(manager, workspace_path=str(project), execution_environment='local')
            assert len(observed) == 1
            context = observed[0]
            assert context.session_id == root.id
            assert context.root == project
            assert context.root_identity == (project.stat().st_dev, project.stat().st_ino)
            assert context.execution_environment == 'local'
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_prepared_context_and_actual_sdk_request_share_canonical_catalog(compatibility_schemas,
                                                                      tmp_path, monkeypatch):
    from agent_core.core import llm, sessions
    captured = {}
    monkeypatch.setattr(llm, 'TOOL_SCHEMAS', compatibility_schemas)
    monkeypatch.setattr(sessions, 'TOOL_SCHEMAS', compatibility_schemas)
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', tmp_path / 'supervisor.env')

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **_kwargs):
            return self

        async def create(self, **kwargs):
            captured['request'] = kwargs['tools']

            async def stream():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    finish_reason='stop', delta=SimpleNamespace(
                        content='done', reasoning_content=None, tool_calls=None))])
            return stream()

    monkeypatch.setattr(llm, 'endpoint_client', lambda _endpoint: Client())
    monkeypatch.setattr(llm, 'model_options', lambda: {})
    monkeypatch.setattr(llm, 'selected_model', lambda: 'controlled-schema-client')
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def run():
        manager = SessionManager(store, model=llm.call_model)
        try:
            async def prepare(session, history, *, schemas, **_kwargs):
                captured['context'] = schemas
                return history, {'structured': True}
            monkeypatch.setattr(manager.history_context, 'prepare', prepare)
            parent = manager.create()
            parent.record['tool_names'] = ['create_session', 'delegate', 'wait_agent_task',
                'task_status', 'inspect_command_job', 'job_status', 'job_logs']
            manager.submit(parent, '检查模型目录')
            await asyncio.wait_for(parent.task, 10)
            assert parent.record['status'] == 'idle'
            assert captured['context'] == captured['request']
            assert set(names(captured['request'])) == {'create_session', 'wait_agent_task', 'inspect_command_job'}
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()
