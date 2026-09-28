import pytest

from ai_agent_startup import config


def test_budget_reserves_concurrently_and_persists(tmp_path, monkeypatch):
    from ai_agent_startup.core.budgets import BudgetLedger, BudgetExceeded
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1.0, raising=False)
    monkeypatch.setattr(config, 'DAILY_COST_LIMIT', 1.5, raising=False)
    ledger = BudgetLedger(tmp_path)
    ticket = ledger.reserve('a', 0.75)
    with pytest.raises(BudgetExceeded):
        ledger.reserve('a', 0.5)
    ledger.settle(ticket, 0.25)
    restored = BudgetLedger(tmp_path)
    restored.reserve('a', 0.75)
    with pytest.raises(BudgetExceeded):
        restored.reserve('b', 0.6)


def test_rate_limit_applies_to_requests_and_recovers(tmp_path, monkeypatch):
    from ai_agent_startup.core import budgets
    monkeypatch.setattr(config, 'MODEL_REQUESTS_PER_MINUTE', 2, raising=False)
    clock = [100.0]
    monkeypatch.setattr(budgets.time, 'time', lambda: clock[0])
    ledger = budgets.BudgetLedger(tmp_path)
    ledger.reserve('a', 0)
    ledger.reserve('b', 0)
    with pytest.raises(budgets.BudgetExceeded):
        ledger.reserve('c', 0)
    clock[0] += 61
    ledger.reserve('c', 0)


def test_cost_limit_refuses_unknown_price(tmp_path, monkeypatch):
    from ai_agent_startup.core.budgets import BudgetLedger, BudgetExceeded
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1, raising=False)
    with pytest.raises(BudgetExceeded, match='单价'):
        BudgetLedger(tmp_path).reserve('a', None)


def test_budget_stops_full_session_before_sdk_request(tmp_path, monkeypatch):
    import asyncio
    from ai_agent_startup.core.sessions import SessionManager
    from ai_agent_startup.core.storage import SessionStore
    from ai_agent_startup.core import llm
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 0.001)
    monkeypatch.setattr(config, 'INPUT_COST_PER_MILLION', 1)
    monkeypatch.setattr(config, 'OUTPUT_COST_PER_MILLION', 1)
    monkeypatch.setattr(config, 'MODEL_PARAMETERS', {})
    class Client:
        def with_options(self, **kwargs):
            pytest.fail('预算不足时不能向 SDK 发请求')
    monkeypatch.setattr(llm, 'endpoint_client', lambda _: Client())
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    async def run():
        manager = SessionManager(store)
        session = manager.create()
        manager.submit(session, 'test budget')
        await session.task
        await manager.flush()
        assert session.record['status'] == 'checkpoint'
        assert session.record['last_run']['stop_reason'] == 'cost_or_rate_budget'
        assert 'SESSION_COST_LIMIT' in session.record['messages'][-1]['content']
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()
