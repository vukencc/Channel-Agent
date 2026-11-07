"""Windows 原生项目、工作区与 Flow 持久化回归。"""
import asyncio
import os
from pathlib import Path

import pytest

from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='验证真实 Windows 文件系统')


def test_native_project_flow_survives_save_run_and_restart(tmp_path):
    project_path = tmp_path / '中文 项目'
    project_path.mkdir()

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': '已处理本轮输入'}

    async def run():
        store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspaces')
        manager = SessionManager(store, model=model)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        try:
            project = projects.create('Windows 项目', str(project_path), permission_policy='readonly')
            flow = flows.get(project['flow_id'])
            flow['nodes'] = [{'id': 'first', 'title': '节点', 'prompt': '处理输入', 'reuse_session': True}]
            flows.save(flow)
            flows.start(flow['id'], input_text='第一批')
            await flows.tasks[flow['id']]
            first = flows.get(flow['id'])
            assert first['run']['status'] == 'completed', first['run']['error']
            flows.start(flow['id'], input_text='第二批')
            await flows.tasks[flow['id']]
            second = flows.get(flow['id'])
            assert second['run']['status'] == 'completed', second['run']['error']
            assert first['run']['nodes']['first']['session_id'] == second['run']['nodes']['first']['session_id']
            await manager.flush()
            return project['id'], flow['id']
        finally:
            await flows.shutdown()
            await manager.shutdown()
            store.close()

    project_id, flow_id = asyncio.run(run())
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspaces')
    manager = SessionManager(store, model=model)
    flows = FlowService(manager)
    projects = ProjectService(manager, flows)
    try:
        assert projects.get(project_id)['workspace_path'] == str(project_path)
        assert flows.get(flow_id)['run']['status'] == 'completed'
    finally:
        store.close()


def test_windows_workspace_rejects_drive_relative_and_private_paths(tmp_path):
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspaces')
    manager = SessionManager(store)
    try:
        private = tmp_path / '.AgEnT'
        private.mkdir()
        for path in ('D:relative', str(private), str(tmp_path / 'file.txt:secret')):
            with pytest.raises((ValueError, PermissionError, OSError)):
                manager.workspaces.validate_path(path)
    finally:
        store.close()
