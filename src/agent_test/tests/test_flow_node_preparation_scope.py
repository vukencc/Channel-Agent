"""节点同步准备的工作区句柄作用域；模型为受控替身。"""
import asyncio
import builtins
from contextlib import contextmanager, nullcontext
import os
import sys
import types

import pytest

from agent_core.core import flows
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


class WindowsOS:
    name = 'nt'

    def __getattr__(self, name):
        return getattr(os, name)


def observed_scope(original, project, events, active):
    @contextmanager
    def scope(path):
        with original(path):
            if path != project:
                yield
                return
            events.append('enter')
            active.append(path)
            try:
                yield
            finally:
                active.pop()
                events.append('exit')
    return scope


def test_observed_scope_delegates_all_prefixes(tmp_path):
    calls, events, active = [], [], []
    project = tmp_path / 'project'

    @contextmanager
    def original(path):
        calls.append(('enter', path))
        try:
            yield
        finally:
            calls.append(('exit', path))

    scope = observed_scope(original, project, events, active)
    for path in (project, tmp_path / 'other'):
        with scope(path):
            assert active == ([project] if path == project else [])
    assert calls == [('enter', project), ('exit', project),
                     ('enter', tmp_path / 'other'), ('exit', tmp_path / 'other')]
    assert events == ['enter', 'exit'] and not active


@pytest.mark.parametrize('fail', [False, True])
def test_windows_node_scope_ends_before_model_and_drain(tmp_path, monkeypatch, fail):
    project = tmp_path / 'project'
    project.mkdir()
    events, active, created = [], [], []

    async def model(history, **kwargs):
        assert not active
        events.append('model')
        return {'role': 'assistant', 'content': '完成'}

    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'private')
        manager = SessionManager(store, model=model)
        service = flows.FlowService(manager)
        value = service.create(workspace_path=str(project))
        value['nodes'] = [{'id': 'a', 'title': 'A', 'prompt': 'A'},
                          {'id': 'b', 'title': 'B', 'prompt': 'B'}]
        value['edges'] = [{'source': 'a', 'target': 'b'}]
        value = service.save(value)
        original_validate = manager.workspaces.validate_path
        original_node = service._node_session
        original_submit = manager.submit
        original_drain = service._drain

        def validate(path, *args, **kwargs):
            if active and events[-1] == 'enter':
                events.append('validate')
            return original_validate(path, *args, **kwargs)

        def node(*args, **kwargs):
            assert active == [project]
            events.append('node')
            result = original_node(*args, **kwargs)
            created.append(result[0])
            return result

        def submit(*args, **kwargs):
            assert active == [project]
            events.append('submit')
            return original_submit(*args, **kwargs)

        async def drain(session, *args, **kwargs):
            assert not active
            assert session in created
            events.append('drain')
            return await original_drain(session, *args, **kwargs)

        def fail_configuration(*args, **kwargs):
            assert active == [project] and created
            raise RuntimeError('controlled configuration failure')

        monkeypatch.setattr(manager.workspaces, 'validate_path', validate)
        monkeypatch.setattr(service, '_node_session', node)
        monkeypatch.setattr(manager, 'submit', submit)
        monkeypatch.setattr(service, '_drain', drain)
        if fail:
            monkeypatch.setattr(manager, 'set_permission_policy', fail_configuration)
        if os.name == 'nt':
            from agent_core.core import windows_files
            scope = observed_scope(windows_files.pinned_scope, project, events, active)
            monkeypatch.setattr(windows_files, 'pinned_scope', scope)
        else:
            scope = observed_scope(lambda path: nullcontext(), project, events, active)
            monkeypatch.setitem(sys.modules, 'agent_core.core.windows_files', types.SimpleNamespace(pinned_scope=scope))
        monkeypatch.setattr(flows, 'os', WindowsOS())
        try:
            service.start(value['id'])
            await service.tasks[value['id']]
            result = service.get(value['id'])['run']
            assert result['status'] == ('failed' if fail else 'completed'), result
            assert not active
            if fail:
                assert events == ['enter', 'validate', 'node', 'exit', 'drain']
                assert not manager.project_reservations
            else:
                assert events == ['enter', 'validate', 'node', 'submit', 'exit', 'model', 'drain'] * 2
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())


@pytest.mark.skipif(os.name == 'nt', reason='验证 Linux 分支')
def test_linux_node_preparation_does_not_import_windows_scope(tmp_path, monkeypatch):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': '完成'}

    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        store = SessionStore(tmp_path / 'state', tmp_path / 'private')
        manager = SessionManager(store, model=model)
        service = flows.FlowService(manager)
        value = service.create(workspace_path=str(project))
        value['nodes'] = [{'id': 'a', 'title': 'A', 'prompt': 'A'}]
        value = service.save(value)
        original_import = builtins.__import__
        def checked_import(name, *args, **kwargs):
            if name == 'agent_core.core.windows_files':
                pytest.fail('Linux 不应导入 Windows 作用域')
            return original_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', checked_import)
        try:
            service.start(value['id'])
            await service.tasks[value['id']]
            assert service.get(value['id'])['run']['status'] == 'completed'
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())
