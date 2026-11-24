import pytest

from agent_core import config


def test_budget_reserves_concurrently_and_persists(tmp_path, monkeypatch):
    from agent_core.core.budgets import BudgetLedger, BudgetExceeded
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
    from agent_core.core import budgets
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
    from agent_core.core.budgets import BudgetLedger, BudgetExceeded
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1, raising=False)
    with pytest.raises(BudgetExceeded, match='单价'):
        BudgetLedger(tmp_path).reserve('a', None)


def test_budget_stops_full_session_before_sdk_request(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    from agent_core.core import llm
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


def test_parallel_flow_reviews_reserve_each_request_before_dispatch(tmp_path, monkeypatch):
    """两个真实来源复核请求竞争同一费用额度；供应商传输替换为离线流。"""
    import asyncio
    import threading
    from types import SimpleNamespace

    from agent_core.core import llm
    from agent_core.core.budgets import BudgetExceeded, BudgetLedger, budget_scope
    from agent_core.core.tool_settings import model_tools
    from agent_test.tests.test_flow_draft_receipt_review import make_gate
    from agent_test.tests.test_flow_whole_claim_batches import draft_with_references
    from agent_test.tests.test_llm_stream import FakeStream, chunk

    monkeypatch.setattr(config, 'FLOW_REVIEW_CONCURRENCY', 2, raising=False)
    monkeypatch.setattr(config, 'MODEL', 'offline-fixture')
    monkeypatch.setattr(config, 'MODEL_PARAMETERS', {'max_tokens': 4096})
    monkeypatch.setattr(config, 'INPUT_COST_PER_MILLION', 0.01)
    monkeypatch.setattr(config, 'OUTPUT_COST_PER_MILLION', 1.0)
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 0.006)
    denied = threading.Event()
    requests = []
    metrics = []

    class ObservedLedger(BudgetLedger):
        def reserve(self, session_id, cost):
            try:
                return super().reserve(session_id, cost)
            except BudgetExceeded:
                denied.set()
                raise

    class OfflineEndpoint:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **options):
            return self

        async def create(self, **request):
            requests.append(request)
            # 第一笔仍占用预留时，第二笔就必须得到真实账本的拒绝。
            assert await asyncio.to_thread(denied.wait, 2)
            return FakeStream([chunk(content='{"status":"pass"}', finish='stop')])

    monkeypatch.setattr(llm, 'endpoint_client', lambda endpoint: OfflineEndpoint())
    gate, _, _ = make_gate(monkeypatch)
    ledger = ObservedLedger(tmp_path)

    async def review(history, *, disable_tools, session_id, emit):
        assert disable_tools
        with model_tools([], []):
            return await llm.call_model(history, session_id=session_id, emit=emit)

    gate.manager._call_model = review
    gate.manager.emit = lambda session, kind, data: metrics.append(data)
    with budget_scope(ledger, gate.session.id):
        assert asyncio.run(gate.check(draft_with_references(), stage='handoff'))

    assert denied.is_set()
    assert len(requests) == 1 and 'tools' not in requests[0]
    assert sorted(row['outcome'] for row in metrics) == ['budget_exceeded', 'ok']
    event = gate.session.record['last_run']['draft_reviews'][-1]
    assert sorted(row['status'] for row in event['batches']) == ['BudgetExceeded', 'passed']
    # 无供应商 usage 的成功流按预留结算。被拒的另一笔没有网络请求或费用。
    totals = BudgetLedger(tmp_path).state
    assert totals['pending'] == {}
    assert set(totals['sessions']) == {gate.session.id}
    assert 0.004096 < totals['sessions'][gate.session.id] < config.SESSION_COST_LIMIT
    assert sum(totals['days'].values()) == totals['sessions'][gate.session.id]


def test_retired_review_waiting_for_model_slot_never_calls_model(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core.model_settings import ModelDispatchStopped, model_dispatch_guard
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    monkeypatch.setattr(config, 'MAX_CONCURRENT_AGENTS', 1)
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    called = []

    async def model(*args, **kwargs):
        called.append(True)
        return {'role': 'assistant', 'content': 'ok'}

    async def run():
        manager = SessionManager(store, model=model)
        session = manager.create()
        current = [True]
        try:
            async with manager.slots:
                with model_dispatch_guard(lambda: current[0]):
                    queued = asyncio.create_task(manager._call_model([], disable_tools=True, session_id=session.id))
                await asyncio.sleep(0)
                current[0] = False
            with pytest.raises(ModelDispatchStopped):
                await queued
            assert not called and manager.concurrency_status()['active_model_calls'] == 0
            await manager._call_model([], disable_tools=True, session_id=session.id)
            assert called == [True]
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_review_retires_during_reservation_refunds_unsent_request(tmp_path, monkeypatch):
    import asyncio
    from agent_core.core import llm
    from agent_core.core.budgets import BudgetLedger, budget_scope
    from agent_core.core.model_settings import ModelDispatchStopped, model_dispatch_guard

    current = [True]

    class ObservedLedger(BudgetLedger):
        def reserve(self, session_id, cost):
            ticket = super().reserve(session_id, cost)
            current[0] = False
            return ticket

    class NoDispatch:
        def with_options(self, **kwargs):
            pytest.fail('已经退回的草稿不能发送下一笔请求')

    ledger = ObservedLedger(tmp_path)
    monkeypatch.setattr(llm, 'endpoint_client', lambda _: NoDispatch())
    monkeypatch.setattr(llm, 'reserved_cost', lambda *args: 0.75)
    events = []
    with budget_scope(ledger, 'same-node'), model_dispatch_guard(lambda: current[0]):
        with pytest.raises(ModelDispatchStopped):
            asyncio.run(llm.call_model([], emit=lambda kind, data: events.append((kind, data))))
    state = BudgetLedger(tmp_path).state
    assert not state['pending'] and state['sessions']['same-node'] == 0
    assert sum(state['days'].values()) == 0
    assert events[-1][1]['request_attempts'] == 0
    assert events[-1][1]['outcome'] == 'not_dispatched'


@pytest.mark.parametrize('decline', [False, True])
def test_cancel_during_durable_reservation_leaves_no_orphan_charge(tmp_path, decline):
    import asyncio
    import threading
    from agent_core.core.budgets import BudgetLedger, budget_scope, reserve_request

    started, release = threading.Event(), threading.Event()

    class DelayedLedger(BudgetLedger):
        def reserve(self, session_id, cost):
            started.set()
            assert release.wait(3)
            if decline:
                from agent_core.core.budgets import BudgetExceeded
                raise BudgetExceeded('该预留未获批准')
            return super().reserve(session_id, cost)

    ledger = DelayedLedger(tmp_path)

    async def run():
        with budget_scope(ledger, 'node'):
            task = asyncio.create_task(reserve_request(0.4))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    state = BudgetLedger(tmp_path).state
    assert not state['pending'] and state['sessions'].get('node', 0) == 0
    assert sum(state['days'].values()) == 0


def test_retired_transport_retry_preserves_first_attempt_and_its_reservation(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import httpx
    from agent_core.core import llm
    from agent_core.core.budgets import BudgetLedger, budget_scope
    from agent_core.core.model_settings import ModelDispatchStopped, model_dispatch_guard

    received = []

    class InterruptedEndpoint:
        def with_options(self, **options):
            self.chat = SimpleNamespace(completions=self)
            return self

        async def create(self, **request):
            received.append(request)
            raise httpx.ReadError('离线传输在首帧前中断')

    ledger = BudgetLedger(tmp_path)
    monkeypatch.setattr(config, 'MAX_RETRIES', 2)
    monkeypatch.setattr(llm, 'endpoint_client', lambda _: InterruptedEndpoint())
    monkeypatch.setattr(llm, 'reserved_cost', lambda *args: 0.4)
    events = []
    with budget_scope(ledger, 'same-node'), model_dispatch_guard(lambda: not received):
        with pytest.raises(ModelDispatchStopped):
            asyncio.run(llm.call_model([], emit=lambda kind, data: events.append((kind, data))))
    state = BudgetLedger(tmp_path).state
    assert len(received) == 1 and events[-1][1]['request_attempts'] == 1
    assert events[-1][1]['outcome'] == 'dispatch_stopped'
    assert not state['pending'] and state['sessions']['same-node'] == 0.4
