import asyncio
import copy

import pytest

from agent_core import config
from agent_core.core.history_context import ContextBudgetError
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.fixture(autouse=True)
def structured_context(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", True)


def schemas(*names):
    return [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}} for name in names]


@pytest.mark.parametrize("names", [(), ("history_search",), ("history_read",), ("history_search", "history_read")])
def test_current_schema_controls_history_guidance(tmp_path, sandbox_env, names):
    async def run():
        store = SessionStore(tmp_path / "state", sandbox_env)
        manager = SessionManager(store)
        session = manager.create()
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "current"}]
        try:
            supplied = schemas(*names)
            before = copy.deepcopy(supplied)
            history, _ = await manager.history_context.prepare(session, messages, schemas=supplied)
            guide = history[0]["content"]
            assert ("history_search" in guide) == ("history_search" in names)
            assert ("history_read" in guide) == ("history_read" in names)
            if "history_read" not in names:
                assert "不能使用历史工具取回已归档原文" in guide
            assert "不要重放已执行操作" in guide
            assert supplied == before
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_capabilities_change_on_next_prepare_and_summary_truncation(tmp_path, sandbox_env):
    async def run():
        store = SessionStore(tmp_path / "state", sandbox_env)
        manager = SessionManager(store)
        session = manager.create()
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "current"}]
        try:
            manifest = await manager.history_context._manifest(session)
            manifest.update(session_summary="摘要" * 100, working_summary_chars=60)
            first, _ = await manager.history_context.prepare(session, messages, schemas=schemas("history_search", "history_read", "read_file", "rag_search"))
            second, _ = await manager.history_context.prepare(session, messages, schemas=[])
            assert all(name in first[0]["content"] for name in ("history_search", "history_read", "read_file", "rag_search"))
            assert all(name not in second[0]["content"] for name in ("history_search", "history_read", "read_file", "rag_search"))
            assert "完整摘要及原文可检索" not in second[1]["content"]
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_hard_budget_preserves_current_pair_and_runtime_receipt(tmp_path, sandbox_env, monkeypatch):
    monkeypatch.setattr(config, "MODEL_INPUT_CHARS", 23000)
    monkeypatch.setattr(config, "MODEL_INPUT_TOKENS", 100000)
    monkeypatch.setattr(config, "CONTEXT_SUMMARY_CHARS", 128)
    async def run():
        store = SessionStore(tmp_path / "state", sandbox_env)
        manager = SessionManager(store)
        session = manager.create()
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "x" * 30000}, {"role": "user", "content": "CURRENT"},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "create_file", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c", "content": "ok"}]
        try:
            history, metrics = await manager.history_context.prepare(session, messages, schemas=schemas("create_file"), runtime="SUCCESS_RECEIPT")
            assert metrics["archived_chunks"] > 0
            assert "SUCCESS_RECEIPT" in history[0]["content"]
            assert any("CURRENT" in m.get("content", "") for m in history)
            assert any(m.get("tool_call_id") == "c" for m in history)
            assert any(any(c["id"] == "c" for c in m.get("tool_calls", [])) for m in history)
            monkeypatch.setattr(config, "MODEL_INPUT_CHARS", 100)
            with pytest.raises(ContextBudgetError):
                await manager.history_context.prepare(session, messages, schemas=schemas("create_file"), runtime="SUCCESS_RECEIPT")
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())
