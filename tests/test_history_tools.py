"""Model-visible history tools keep private ownership and bounded JSON output."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.session_service import session_service_context
from ai_agent_startup.tools import TOOL_REGISTRY


def _register_history_tools():
    from ai_agent_startup.tools import context_history
    importlib.reload(context_history)
    return context_history.history_search, context_history.history_read


@pytest.fixture(autouse=True)
def restore_registry():
    previous = dict(TOOL_REGISTRY)
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(previous)


def test_history_tools_require_live_session_caller_and_reject_extra_identity_args():
    _register_history_tools()
    search = TOOL_REGISTRY['history_search']
    read = TOOL_REGISTRY['history_read']
    with pytest.raises(PermissionError):
        search.run(json.dumps({'query': 'needle'}))
    with pytest.raises(PermissionError):
        read.run(json.dumps({'chunk_id': 'a' * 32}))
    with pytest.raises(ValueError):
        search.run(json.dumps({'query': 'needle', 'session_id': 'other'}))
    with pytest.raises(ValueError):
        read.run(json.dumps({'chunk_id': 'a' * 32, 'before': 2, 'after': 2}))


def test_history_search_uses_context_caller_and_returns_parseable_summary_only_page(monkeypatch):
    async def run():
        _register_history_tools()
        owner = SimpleNamespace(id='owner')
        seen = []

        class FakeHistory:
            async def search(self, session, query, *, limit, method):
                seen.append((session, query, limit, method))
                return [{'ID': 'a' * 32, 'Summary': 'S' * 1500,
                         'score': 1.0, 'RawHistory': 'private raw value'}]

        manager = SimpleNamespace(history_context=FakeHistory(), sessions={'owner': owner}, closing=False)
        monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 300)
        with session_service_context(manager, 'owner', asyncio.get_running_loop()):
            payload = await asyncio.to_thread(
                TOOL_REGISTRY['history_search'].run,
                json.dumps({'query': 'needle', 'limit': 1, 'method': 'bm25'}))
        result = json.loads(payload)
        assert len(payload) <= 300
        assert result[0]['ID'] == 'a' * 32
        assert 'RawHistory' not in result[0]
        assert seen == [(owner, 'needle', 1, 'bm25')]

    asyncio.run(run())
