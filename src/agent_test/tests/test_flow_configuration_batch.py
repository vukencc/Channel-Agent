"""Flow 配置合并保存的持久化边界。"""

import asyncio
import copy
from contextlib import contextmanager

import pytest

from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService


def test_configuration_batch_saves_once_and_public_setters_save(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    manager = SessionManager(store)
    session = manager.create("测试")
    session.record["flow_id"] = "flow"
    saves = []
    original = manager.save
    monkeypatch.setattr(manager, "save", lambda value: (saves.append(value.id), original(value))[1])
    try:
        with manager.configuration_batch(session):
            manager.set_permission_policy(session, "readonly")
            manager.set_model_profile(session, None)
            manager.set_custom_tools(session, [])
            manager.set_tool_names(session, None)
        assert saves == [session.id]
        manager.set_permission_policy(session, None)
        assert saves == [session.id, session.id]
    finally:
        store.close()


def test_failure_flushes_partial_changes_and_resets_batch(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    manager = SessionManager(store)
    session = manager.create("测试")
    other = manager.create("其他")
    saves = []
    original = manager.save
    monkeypatch.setattr(manager, "save", lambda value: (saves.append(value.id), original(value))[1])
    try:
        with pytest.raises(ValueError):
            with manager.configuration_batch(session):
                manager.set_permission_policy(session, "readonly")
                manager.set_permission_policy(other, "smart")
                with pytest.raises(RuntimeError):
                    with manager.configuration_batch(session):
                        pass
                manager.set_permission_policy(session, "invalid")
        assert saves == [other.id, session.id]
        assert store.load_record(store.directory(session.id) / "session.json")["permission_policy"] == "readonly"
        assert store.load_record(store.directory(other.id) / "session.json")["permission_policy"] == "smart"
        manager.set_permission_policy(session, None)
        assert saves == [other.id, session.id, session.id]
    finally:
        store.close()


def test_flow_reused_node_configuration_saves_once(tmp_path, monkeypatch):
    async def run():
        async def model(history, *, session_id, emit):
            return {"role": "assistant", "content": "完成"}

        project_path = tmp_path / "project"
        project_path.mkdir()
        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        projects = ProjectService(manager, service)
        original_batch, original_save = manager.configuration_batch, manager.save
        saves, counts = [], []
        monkeypatch.setattr(manager, "save", lambda session: (saves.append(copy.deepcopy(session.record)), original_save(session))[1])

        @contextmanager
        def observed_batch(session):
            before = len(saves)
            with original_batch(session):
                yield
            counts.append((session.id, saves[before:]))

        monkeypatch.setattr(manager, "configuration_batch", observed_batch)
        try:
            project = projects.create("项目", str(project_path))
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "one", "title": "节点", "prompt": "执行", "reuse_session": True}]
            flow["edges"] = []
            flow = service.save(flow)
            for text in ("甲", "乙"):
                service.start(flow["id"], input_text=text)
                await asyncio.wait_for(service.tasks[flow["id"]], 5)
                assert service.get(flow["id"])["run"]["status"] == "completed"
            assert len(counts) == 2
            assert counts[0][0] == counts[1][0]
            for session_id, snapshots in counts:
                assert len(snapshots) == 1
                snapshot = snapshots[0]
                assert snapshot['id'] == session_id and snapshot['status'] == 'queued'
                assert snapshot['flow_node_id'] == 'one'
                assert snapshot['permission_policy'] is None
                assert snapshot['custom_tools'] == []
                assert snapshot['messages'][-1]['role'] == 'user'
                assert snapshot['last_run']['input_index'] == len(snapshot['messages']) - 1
        finally:
            await service.shutdown()
            await manager.shutdown()
            store.close()

    asyncio.run(run())


def test_configuration_change_after_snapshot_is_flushed(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    manager = SessionManager(store)
    session = manager.create("测试")
    original = manager.save
    snapshots = []
    monkeypatch.setattr(manager, "save", lambda value: (snapshots.append(value.record['permission_policy']), original(value))[1])
    try:
        with manager.configuration_batch(session):
            manager.set_permission_policy(session, "readonly")
            manager.save(session)
            manager.set_permission_policy(session, "smart")
        assert snapshots == ["readonly", "smart"]
        assert store.load_record(store.directory(session.id) / "session.json")['permission_policy'] == 'smart'
    finally:
        store.close()


@pytest.mark.parametrize('failure', ['validation', 'handoff', 'queued_write'])
def test_flow_setup_failure_never_starts_model(tmp_path, monkeypatch, failure):
    async def run():
        calls = []
        async def model(history, *, session_id, emit):
            calls.append(session_id)
            (tmp_path / 'unexpected').write_text('executed')
            return {'role': 'assistant', 'content': '完成'}

        project_path = tmp_path / 'project'
        project_path.mkdir()
        store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'private')
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        projects = ProjectService(manager, service)
        snapshots = []
        original_save = manager.save
        monkeypatch.setattr(manager, 'save', lambda session: (snapshots.append(copy.deepcopy(session.record)), original_save(session))[1])
        try:
            project = projects.create('项目', str(project_path))
            flow = service.get(project['flow_id'])
            node = {'id': 'one', 'title': '节点', 'prompt': '执行'}
            if failure == 'handoff':
                node['handoff_file'] = 'missing.txt'
            flow['nodes'], flow['edges'] = [node], []
            flow = service.save(flow)
            if failure == 'validation':
                def invalid(*args):
                    raise ValueError('配置故障')
                monkeypatch.setattr(manager, 'set_custom_tools', invalid)
            elif failure == 'queued_write':
                original_store_save = store.save
                def fail_queued(record):
                    if record['status'] == 'queued':
                        raise OSError('受控写入故障')
                    return original_store_save(record)
                monkeypatch.setattr(store, 'save', fail_queued)
            service.start(flow['id'], input_text='输入')
            await asyncio.wait_for(service.tasks[flow['id']], 5)
            await manager.flush()
            assert service.get(flow['id'])['run']['status'] != 'completed'
            assert not calls and not (tmp_path / 'unexpected').exists()
            configured = [record for record in snapshots if record.get('flow_node_id') == 'one']
            assert configured
            if failure != 'queued_write':
                assert configured[0]['status'] == 'idle'
                assert configured[0]['permission_policy'] is None
            else:
                assert configured[0]['status'] == 'queued'
            assert manager._configuration_batch_session is None
            assert not manager._configuration_batch_dirty
        finally:
            await service.shutdown()
            await manager.shutdown()
            store.close()

    asyncio.run(run())
