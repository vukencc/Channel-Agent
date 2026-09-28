import asyncio
from types import SimpleNamespace
from functools import partial

import config
from core import llm
from core.context import prepare_model_history
from core.sessions import SessionManager
from core.storage import SessionStore


def test_auxiliary_model_and_timeout_reach_sdk(monkeypatch):
    captured = []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            captured.append(kwargs)
            return self
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='摘要测试桩'))])
    monkeypatch.setattr(llm, 'get_client', lambda: Client())
    monkeypatch.setattr(config, 'AUX_MODEL', 'configured-small-model', raising=False)
    monkeypatch.setattr(config, 'AUX_TIMEOUT', 3, raising=False)
    assert asyncio.run(llm.complete('合成测试')) == '摘要测试桩'
    assert captured[0]['timeout'] == 3
    assert captured[1]['model'] == 'configured-small-model'


def test_background_summary_returns_before_judge_and_reuses_cache(tmp_path, monkeypatch):
    import core.sessions as sessions
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1800)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY', True)
    messages = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '订单 A17'},
                {'role': 'assistant', 'content': 'x' * 2000}, {'role': 'user', 'content': '继续'}]
    async def run():
        release, entered = asyncio.Event(), asyncio.Event()
        async def judge(prompt, **kwargs):
            entered.set()
            await release.wait()
            return '订单 A17'
        monkeypatch.setattr(sessions, 'complete', judge)
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        session = manager.create()
        try:
            _, metrics = await prepare_model_history(messages, judge=judge, cache=session.context_summaries,
                                                       schedule_summary=partial(manager._schedule_summary, session))
            assert metrics['summary'] == 'pending'
            await asyncio.wait_for(entered.wait(), 1)
            release.set()
            await session.summary_task
            _, metrics = await prepare_model_history(messages, judge=judge, cache=session.context_summaries,
                                                       schedule_summary=partial(manager._schedule_summary, session))
            assert metrics['summary'] == 'applied'
        finally:
            release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_unknown_auxiliary_price_cannot_bypass_budget(tmp_path, monkeypatch):
    import pytest
    from core.budgets import BudgetLedger, BudgetExceeded, budget_scope
    monkeypatch.setattr(config, 'AUX_MODEL', 'other-model')
    monkeypatch.setattr(config, 'AUX_INPUT_COST_PER_MILLION', 0)
    monkeypatch.setattr(config, 'AUX_OUTPUT_COST_PER_MILLION', 0)
    monkeypatch.setattr(config, 'INPUT_COST_PER_MILLION', 1)
    monkeypatch.setattr(config, 'OUTPUT_COST_PER_MILLION', 1)
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1)
    class Client:
        def with_options(self, **kwargs):
            raise AssertionError('未知价格时不能发送请求')
    monkeypatch.setattr(llm, 'get_client', lambda: Client())
    async def run():
        with budget_scope(BudgetLedger(tmp_path), 'session'):
            with pytest.raises(BudgetExceeded, match='单价未知'):
                await llm.complete('合成测试')
    asyncio.run(run())


def test_busy_auxiliary_pool_does_not_block_main_model(tmp_path, monkeypatch):
    import core.sessions as sessions
    monkeypatch.setattr(config, 'AUX_CONCURRENCY', 1)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def auxiliary(prompt, **kwargs):
            entered.set()
            await release.wait()
            return '摘要桩'
        async def main(history, **kwargs):
            return '主请求桩'
        monkeypatch.setattr(sessions, 'complete', auxiliary)
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store, model=main)
        task = asyncio.create_task(manager._complete('摘要', session_id='synthetic'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert await asyncio.wait_for(manager._call_model([], session_id='synthetic'), .5) == '主请求桩'
        finally:
            release.set()
            await task
            await manager.shutdown()
            store.close()
    asyncio.run(run())
