import asyncio
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from agent_core.core.budgets import BudgetExceeded, active_budget
from agent_core.core.model_settings import active_endpoint, active_profile
from agent_core.core.runtime_models import RuntimeModelSnapshot
from agent_core.core.session_limits import context_budget_metadata
from agent_core.core.tool_settings import selected_schemas


def manager_for(model):
    endpoint = RuntimeModelSnapshot(id="profile", name="test", model="test-model", base_url="https://private.invalid", api_key="secret", proxy_mode="windows_system")
    registry = SimpleNamespace(contains=lambda name: name == "profile", resolve=lambda name: endpoint)
    return SimpleNamespace(model=model, model_registry=registry, budget_ledger=object(), slots=asyncio.Semaphore(1)), endpoint


def test_connection_check_uses_snapshot_scopes_and_hides_response():
    from agent_core.core.model_diagnostics import check_model_connection
    async def run():
        async def model(history, *, session_id, emit):
            assert active_profile.get() == "profile"
            assert active_endpoint.get() is endpoint
            assert active_budget.get() == (manager.budget_ledger, "model-connection-check")
            assert manager.slots.locked()
            assert selected_schemas([{"function": {"name": "test"}}]) == []
            assert context_budget_metadata()["input_limit_tokens"] > 0
            assert session_id is None and len(history) == 1
            emit("content", "private response")
            return {"content": "private response"}
        manager, endpoint = manager_for(model)
        result = await check_model_connection(manager, "profile")
        assert result["ok"] and result["status"] == "ok"
        assert result["model"] == "test-model" and result["proxy_mode"] == "windows_system"
        assert "private" not in json.dumps(result)
        assert not manager.slots.locked()
    asyncio.run(run())


@pytest.mark.parametrize("failure,category", [
    (BudgetExceeded("secret"), "budget"),
    (TimeoutError("secret"), "timeout"),
    (ConnectionError("secret"), "connect"),
    (openai.APIStatusError("secret", response=httpx.Response(401, request=httpx.Request("POST", "https://private.invalid")), body="secret"), "authentication"),
    (openai.APIStatusError("secret", response=httpx.Response(400, request=httpx.Request("POST", "https://private.invalid")), body="secret"), "unsupported_parameters"),
])
def test_connection_check_safe_errors(failure, category):
    from agent_core.core.model_diagnostics import check_model_connection
    async def model(*args, **kwargs):
        raise failure
    async def run():
        manager, _ = manager_for(model)
        result = await check_model_connection(manager, "profile")
        assert not result["ok"] and result["error_category"] == category
        assert "secret" not in json.dumps(result) and "private.invalid" not in json.dumps(result)
        assert not manager.slots.locked()
    asyncio.run(run())


def test_connection_check_cancellation_releases_slot():
    from agent_core.core.model_diagnostics import check_model_connection
    async def run():
        started = asyncio.Event()
        async def model(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        manager, _ = manager_for(model)
        task = asyncio.create_task(check_model_connection(manager, "profile"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager.slots.locked()
    asyncio.run(run())


def test_connection_check_waiting_for_slot_is_bounded(monkeypatch):
    from agent_core import config
    from agent_core.core.model_diagnostics import check_model_connection
    monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', 0.01)
    async def run():
        async def model(*args, **kwargs):
            pytest.fail('请求不得越过并发槽')
        manager, _ = manager_for(model)
        await manager.slots.acquire()
        try:
            result = await check_model_connection(manager, 'profile')
            assert result['error_category'] == 'timeout'
            assert manager.slots.locked()
        finally:
            manager.slots.release()
    asyncio.run(run())


def test_real_call_model_keeps_fallback_isolated_from_concurrent_diagnostic(monkeypatch):
    from agent_core import config
    from agent_core.core import llm
    from agent_core.core.model_diagnostics import check_model_connection

    calls = []
    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, 'done', False):
                raise StopAsyncIteration
            self.done = True
            delta = SimpleNamespace(content='OK', reasoning_content=None, tool_calls=None)
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason='stop', delta=delta)])

        async def close(self):
            pass

    class Client:
        def with_options(self, **kwargs):
            return self

        @property
        def chat(self):
            return SimpleNamespace(completions=self)

        async def create(self, *, model, **kwargs):
            calls.append(model)
            if model == 'primary':
                raise openai.APIConnectionError(request=httpx.Request('POST', 'https://private.invalid'))
            return Stream()

    async def no_delay(_):
        pass

    monkeypatch.setattr(config, 'MODEL', 'primary')
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [{'model': 'fallback'}])
    monkeypatch.setattr(config, 'MAX_RETRIES', 2)
    monkeypatch.setattr(llm, 'endpoint_client', lambda endpoint: Client())
    monkeypatch.setattr(llm.asyncio, 'sleep', no_delay)

    async def run():
        registry = SimpleNamespace(contains=lambda name: True, resolve=lambda name: None)
        manager = SimpleNamespace(model=llm.call_model, model_registry=registry, budget_ledger=None, slots=asyncio.Semaphore(1))
        diagnostic, ordinary = await asyncio.gather(
            check_model_connection(manager),
            llm.call_model([{'role': 'user', 'content': 'test'}], emit=lambda *_: None),
        )
        assert diagnostic['error_category'] == 'connect'
        assert ordinary['content'] == 'OK'
        assert calls.count('primary') == 4
        assert calls.count('fallback') == 1
    asyncio.run(run())
