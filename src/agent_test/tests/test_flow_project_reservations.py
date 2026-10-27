"""Flow 的项目预约在会话共用入口生效，不能只依赖 GUI 按钮。"""
import asyncio

import pytest

from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_reserved_project_rejects_unrelated_session_without_adding_message(tmp_path):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        try:
            session = manager.create(workspace_path=str(project))
            manager.project_reservations = {manager.workspaces.identity(session): 'flow-owner'}
            before = len(session.record['messages'])
            with pytest.raises(ValueError, match='Flow'):
                manager.submit(session, '不能抢占运行中的项目')
            assert len(session.record['messages']) == before
            assert session.task is None
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_flow_owner_and_delegated_child_can_use_reserved_project(tmp_path):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': '受控回复'}

    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store, model=model)
        try:
            parent = manager.create(workspace_path=str(project))
            parent.record['flow_id'] = 'flow-owner'
            child = manager.create(parent=parent)
            manager.project_reservations = {manager.workspaces.identity(parent): 'flow-owner'}
            manager.project_reservation_nodes[manager.workspaces.identity(parent)] = parent.id
            manager.submit(child, '允许同一 Flow 的真实子 Agent')
            await child.task
            assert child.record['status'] == 'idle'
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_old_node_cannot_reuse_persistent_flow_id_during_later_node(tmp_path):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        try:
            previous = manager.create(workspace_path=str(project))
            current = manager.create(workspace_path=str(project))
            previous.record['flow_id'] = current.record['flow_id'] = 'same-flow'
            identity = manager.workspaces.identity(current)
            manager.project_reservations[identity] = 'same-flow'
            manager.project_reservation_nodes[identity] = current.id
            with pytest.raises(ValueError, match='Flow'):
                manager.submit(previous, '旧节点不能借相同 flow_id 抢占')
            assert previous.task is None
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_current_node_cannot_be_resubmitted_while_flow_is_draining(tmp_path):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': '受控节点回复'}

    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store, model=model)
        try:
            session = manager.create(workspace_path=str(project))
            identity = manager.workspaces.identity(session)
            manager.project_reservations[identity] = 'flow'
            manager.project_reservation_nodes[identity] = session.id
            manager.submit(session, '第一次节点启动')
            await session.task
            assert session.record['status'] == 'idle'
            with pytest.raises(ValueError, match='Flow'):
                manager.submit(session, '交接排空期间不得重启当前节点')
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize('reserved_source', [True, False])
def test_reserved_project_cannot_be_changed_or_selected(tmp_path, reserved_source):
    async def run():
        source, target = tmp_path / 'source', tmp_path / 'target'
        source.mkdir()
        target.mkdir()
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        try:
            session = manager.create(workspace_path=str(source))
            identity = manager.workspaces.directory_identity(source if reserved_source else target)
            manager.project_reservations = {identity: 'flow-owner'}
            with pytest.raises(ValueError, match='Flow'):
                manager.set_workspace_path(session, str(target))
            assert manager.workspace(session) == source
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())
