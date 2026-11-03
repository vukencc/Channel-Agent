import pytest

from agent_core import config
from agent_core.core.tool_settings import model_tools, selected_schemas


def test_explicit_model_tools_cannot_exceed_deployment(monkeypatch):
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", ["read_file"])
    schemas = [{"function": {"name": "read_file"}}, {"function": {"name": "write_file"}}]
    with model_tools(["write_file"]):
        assert selected_schemas(schemas) == []


def test_none_model_tools_inherits_deployment(monkeypatch):
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", ["read_file"])
    schemas = [{"function": {"name": "read_file"}}, {"function": {"name": "write_file"}}]
    with model_tools(None):
        assert [row["function"]["name"] for row in selected_schemas(schemas)] == ["read_file"]


def test_deployment_cap_blocks_set_and_actual_execution(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", ["read_file"])
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    try:
        manager = SessionManager(store)
        session = manager.create()
        with pytest.raises(ValueError, match="MODEL_TOOL_NAMES"):
            manager.set_tool_names(session, ["list_files"])
        session.record["tool_names"] = ["list_files"]
        result = asyncio.run(manager._tool(session, {"id": "denied", "function": {"name": "list_files", "arguments": "{}"}}))
        assert "拒绝" in result
    finally:
        store.close()


def test_thinking_options_apply_to_builtin_and_runtime(monkeypatch):
    from types import SimpleNamespace
    from agent_core.core.llm import model_options
    from agent_core.core.model_settings import model_profile, model_thinking
    monkeypatch.setattr(config, "MODEL_PARAMETERS", {})
    monkeypatch.setattr(config, "REASONING_EFFORT", "high")
    monkeypatch.setattr(config, "THINKING_MODE", "enabled")
    for endpoint in (None, SimpleNamespace(options=lambda: {"reasoning_effort": "low"})):
        with model_profile(None, endpoint), model_thinking("xhigh", "disabled"):
            options = model_options()
            assert "reasoning_effort" not in options
            assert options["extra_body"]["thinking"]["type"] == "disabled"
        with model_profile(None, endpoint), model_thinking("minimal", "enabled"):
            assert model_options()["reasoning_effort"] == "minimal"


def test_fork_keeps_full_ancestor_ceiling_after_source_disappears(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    monkeypatch.setattr(config, "ENABLE_SESSION_BRANCHES", True)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    async def run():
        manager = SessionManager(store)
        root = manager.create()
        parent = manager.create()
        root.record["tool_names"] = ["read_file", "list_files"]
        parent.record.update(delegated_from=root.id, tool_names=["read_file", "list_files", "create_file"])
        child = await manager.fork_session(parent)
        assert manager._effective_session_tools(child) == ["read_file", "list_files"]
        assert child.record["tool_names"] == ["read_file", "list_files"]
        del manager.sessions[parent.id]
        assert manager._effective_session_tools(child) == ["read_file", "list_files"]
        async def capture_model(history, **kwargs):
            from agent_core.core.llm import TOOL_SCHEMAS
            return [row['function']['name'] for row in selected_schemas(TOOL_SCHEMAS)]
        manager.model = capture_model
        assert await manager._call_model([], session_id=child.id) == ["read_file", "list_files"]
        result = await manager._tool(child, {"id": "denied", "function": {"name": "create_file", "arguments": "{}"}})
        assert "拒绝" in result
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_tool_ancestor_missing_and_cycle_fail_closed(tmp_path, monkeypatch):
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    try:
        manager = SessionManager(store)
        first, second = manager.create(), manager.create()
        first.record.update(delegated_from="missing", tool_names=["write_file"])
        assert manager._effective_session_tools(first) == []
        first.record["delegated_from"] = second.id
        second.record["delegated_from"] = first.id
        assert manager._effective_session_tools(first) == []
    finally:
        store.close()


def test_unrestricted_fork_snapshot_survives_source_deletion(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    monkeypatch.setattr(config, "ENABLE_SESSION_BRANCHES", True)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    async def run():
        manager = SessionManager(store)
        parent = manager.create()
        child = await manager.fork_session(parent)
        assert child.record["inherited_tool_names"] is None
        await manager.delete_session(parent)
        assert manager._effective_session_tools(child) is None
        manager.set_tool_names(child, [])
        assert manager._effective_session_tools(child) == []
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_deleting_narrowed_ancestor_does_not_restore_tools(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    monkeypatch.setattr(config, "ENABLE_SESSION_BRANCHES", True)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    async def run():
        manager = SessionManager(store)
        root = manager.create()
        manager.set_tool_names(root, ["read_file", "create_file"])
        child = await manager.fork_session(root)
        grandchild = await manager.fork_session(child)
        manager.set_tool_names(root, ["read_file"])
        assert manager._effective_session_tools(grandchild) == ["read_file"]
        await manager.delete_session(root)
        assert manager._effective_session_tools(child) == ["read_file"]
        assert manager._effective_session_tools(grandchild) == ["read_file"]
        assert store.read_record(store.directory(child.id) / "session.json")["inherited_tool_names"] == ["read_file"]
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_failed_snapshot_save_aborts_ancestor_deletion(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    monkeypatch.setattr(config, "ENABLE_SESSION_BRANCHES", True)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    async def run():
        manager = SessionManager(store)
        root = manager.create()
        child = await manager.fork_session(root)
        original = manager.save
        def failing(session):
            if session is child:
                raise OSError("snapshot failed")
            return original(session)
        monkeypatch.setattr(manager, "save", failing)
        with pytest.raises(OSError, match="snapshot failed"):
            await manager.delete_session(root)
        assert root.id in manager.sessions and store.directory(root.id).exists()
        assert not getattr(manager, "_tool_ceiling_archive", False)
        monkeypatch.setattr(manager, "save", original)
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_archive_blocks_tool_changes_and_forks_until_snapshot_saved(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    monkeypatch.setattr(config, "ENABLE_SESSION_BRANCHES", True)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    async def run():
        manager = SessionManager(store)
        root = manager.create()
        child = await manager.fork_session(root)
        reached, release = asyncio.Event(), asyncio.Event()
        original = manager.save
        def delayed(session):
            if session is not child:
                return original(session)
            async def save():
                reached.set()
                await release.wait()
                await original(session)
            return asyncio.create_task(save())
        monkeypatch.setattr(manager, "save", delayed)
        deletion = asyncio.create_task(manager.delete_session(root))
        await reached.wait()
        with pytest.raises(ValueError, match="归档"):
            manager.set_tool_names(child, [])
        with pytest.raises(ValueError):
            await manager.fork_session(child)
        release.set()
        await deletion
        monkeypatch.setattr(manager, "save", original)
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()
