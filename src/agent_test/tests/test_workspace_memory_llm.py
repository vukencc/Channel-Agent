"""辅助摘要的真实连接选择与预算门槛；SDK 为离线受控替身。"""
import asyncio
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core import llm
from agent_core.core.budgets import BudgetExceeded, BudgetLedger, budget_scope
from agent_core.core.model_settings import model_profile
from agent_core.core.runtime_models import RuntimeModelSnapshot


def _client(monkeypatch):
    sent, endpoints, closed = [], [], []
    async def create(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.with_options = lambda **kwargs: client
    async def connect(endpoint):
        endpoints.append(endpoint)
        return client
    async def close(value):
        closed.append(value)
    monkeypatch.setattr(llm, '_runtime_client', connect)
    monkeypatch.setattr(llm, '_close_runtime_client', close)
    monkeypatch.setattr(config, 'AUX_MODEL', None)
    monkeypatch.setattr(config, 'AUX_MODEL_PARAMETERS', {})
    monkeypatch.setattr(config, 'AUX_INPUT_COST_PER_MILLION', 0)
    monkeypatch.setattr(config, 'AUX_OUTPUT_COST_PER_MILLION', 0)
    return sent, endpoints, closed


def _endpoint():
    return RuntimeModelSnapshot('fixture-id', '离线连接', 'selected-model',
                                'https://selected.invalid/v1', 'offline-credential')


def test_auxiliary_uses_selected_runtime_connection_without_environment_model(monkeypatch):
    sent, endpoints, closed = _client(monkeypatch)
    monkeypatch.setattr(config, 'MODEL', None)
    def forbidden():
        raise AssertionError('不得使用未配置环境连接')
    monkeypatch.setattr(llm, 'get_client', forbidden)
    async def run():
        with model_profile('fixture-id', _endpoint()):
            assert await llm.complete('只整理公开项目摘要') == '{}'
    asyncio.run(run())
    assert len(sent) == len(endpoints) == len(closed) == 1
    assert sent[0]['model'] == 'selected-model'
    assert endpoints[0]['base_url'] == 'https://selected.invalid/v1'


def test_unknown_selected_model_price_cannot_borrow_environment_price(tmp_path, monkeypatch):
    sent, endpoints, closed = _client(monkeypatch)
    monkeypatch.setattr(config, 'MODEL', 'different-environment-model')
    monkeypatch.setattr(config, 'INPUT_COST_PER_MILLION', 1)
    monkeypatch.setattr(config, 'OUTPUT_COST_PER_MILLION', 1)
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 10)
    monkeypatch.setattr(config, 'DAILY_COST_LIMIT', 0)
    monkeypatch.setattr(config, 'MODEL_REQUESTS_PER_MINUTE', 0)
    ledger = BudgetLedger(tmp_path)
    async def run():
        with model_profile('fixture-id', _endpoint()), budget_scope(ledger, '1' * 32):
            with pytest.raises(BudgetExceeded):
                await llm.complete('应在网络请求前拒绝未知价格')
    asyncio.run(run())
    assert not sent
    assert len(closed) == len(endpoints)
