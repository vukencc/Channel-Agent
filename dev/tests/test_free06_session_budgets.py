import asyncio

import pytest

import config
from core.llm import ModelResponseError
from core.sessions import SessionManager
from core.storage import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True, raising=False)
    with_store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    yield with_store
    with_store.close()


def test_parallel_session_budgets_are_isolated_and_persist(store):
    from core.session_limits import limit
    observed = {}
    async def model(history, session_id, **kwargs):
        await asyncio.sleep(0)
        observed.setdefault(session_id, []).append(limit('MAX_TOOL_ROUNDS'))
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': str(len(observed[session_id])), 'type': 'function', 'function': {'name': 'missing', 'arguments': '{}'}}]}
    async def run():
        manager = SessionManager(store, model=model)
        first, second = manager.create(), manager.create()
        manager.set_limits(first, {'MAX_TOOL_ROUNDS': 1})
        manager.set_limits(second, {'MAX_TOOL_ROUNDS': 2})
        manager.submit(first, 'a'); manager.submit(second, 'b')
        await asyncio.gather(first.task, second.task)
        assert observed[first.id] == [1]
        assert observed[second.id] == [2, 2]
        assert first.record['status'] == second.record['status'] == 'checkpoint'
        assert store.read_record(store.directory(first.id) / 'session.json')['budget_overrides'] == {'MAX_TOOL_ROUNDS': 1}
        assert limit('MAX_TOOL_ROUNDS') == config.MAX_TOOL_ROUNDS
        await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('recoveries, expected', [(0, 1), (2, 3)])
def test_recovery_limit_is_effective(store, recoveries, expected):
    calls = []
    async def model(*args, **kwargs):
        calls.append(1)
        raise ModelResponseError('测试服务故障')
    async def run():
        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.set_limits(session, {'MODEL_RECOVERY_LIMIT': recoveries})
        manager.submit(session, 'test')
        await session.task
        assert len(calls) == expected
        assert session.record['status'] == 'error'
        await manager.shutdown()
    asyncio.run(run())


def test_limits_reject_invalid_or_security_settings_and_disabled(store, monkeypatch):
    manager = SessionManager(store)
    session = manager.create()
    for value in ({'MAX_TOOL_ROUNDS': 0}, {'TOOL_TIMEOUT': float('nan')}, {'COMMAND_NETWORK': 'allowlist'}, {'MODEL_RECOVERY_LIMIT': 100}):
        with pytest.raises(ValueError):
            manager.set_limits(session, value)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', False)
    with pytest.raises(ValueError):
        manager.set_limits(session, {'MAX_TOOL_ROUNDS': 2})


def test_context_enforces_session_input_limit_without_changing_global(monkeypatch):
    from core.session_limits import limits_scope
    from core.context import build_model_history, ContextBudgetError
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    original = config.MODEL_INPUT_TOKENS
    with limits_scope({'budget_overrides': {'MODEL_INPUT_TOKENS': 128}}):
        with pytest.raises(ContextBudgetError):
            build_model_history([{'role': 'system', 'content': 'system ' * 1000}, {'role': 'user', 'content': 'test'}], schemas=[])
    assert config.MODEL_INPUT_TOKENS == original


def test_cli_config_sets_persisted_budget_and_displays_remaining(store):
    from core.cli import AgentCLI
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as pipe:
        cli = AgentCLI(store, input=pipe, output=DummyOutput())
        cli.handle('/config {"MAX_TOOL_ROUNDS": 3}')
        assert cli.active.record['budget_overrides'] == {'MAX_TOOL_ROUNDS': 3}
        assert 'MAX_TOOL_ROUNDS' in cli.notice
        cli.active.record['last_run'] = {'budget': {'rounds_remaining': 2}}
        assert '剩余模型轮次 2' in cli.status_text()[0][1]
        cli.handle('/config default')
        assert cli.active.record['budget_overrides'] == {}
