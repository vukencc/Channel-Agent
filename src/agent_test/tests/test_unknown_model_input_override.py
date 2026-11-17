"""未知模型容量的显式进程输入预算；不连接服务。"""
import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.model_settings import model_profile
from agent_core.core.runtime_models import RuntimeModelRegistry
from agent_core.core.session_limits import context_budget_metadata, effective_limits, limits_scope


@pytest.mark.parametrize('setting, expected', [(None, 'False'), ('false', 'False'), ('true', 'True')])
def test_process_setting_is_default_off(setting, expected):
    env = os.environ.copy()
    env['PYTHON_DOTENV_DISABLED'] = '1'
    env.pop('ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE', None)
    if setting is not None:
        env['ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE'] = setting
    result = subprocess.run([sys.executable, '-c',
                             'from agent_core import config; print(config.ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE)'],
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == expected


@pytest.fixture
def unknown_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 32000)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 0)
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 0)
    monkeypatch.setattr(config, 'MODEL_RESPONSE_RESERVE_TOKENS', 4096)
    monkeypatch.setattr(config, 'MODEL_PROFILES', {})
    monkeypatch.setattr(config, 'MODEL_PARAMETERS', {})
    monkeypatch.setattr(config, 'ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE', False, raising=False)
    registry = RuntimeModelRegistry(tmp_path / 'state')
    public = registry.add(name='受控连接', model='controlled', base_url='https://example.test/v1')
    return public['id'], registry.resolve(public['id'])


def test_explicit_process_opt_in_preserves_default_and_output_reserve(unknown_endpoint, monkeypatch):
    identifier, endpoint = unknown_endpoint
    record = {'model_profile': identifier}
    assert effective_limits(record, endpoint)['MODEL_INPUT_TOKENS'] == 16000
    monkeypatch.setattr(config, 'ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE', True)
    with limits_scope(record, endpoint):
        result = context_budget_metadata()
        assert result == {'input_limit_tokens': 32000, 'input_limit_chars': 128000,
                          'context_capacity_tokens': None, 'response_reserve_tokens': 4096}
    assert endpoint.options() == {} and endpoint.context_tokens is None


@pytest.mark.parametrize('cap, override, expected', [
    (0, None, 16000), (0, 12000, 12000), (0, 40000, 16000),
    (32000, None, 32000), (32000, 12000, 12000),
    (32000, 40000, 32000), (12000, 40000, 12000),
])
@pytest.mark.parametrize('delegated', [False, True])
def test_opt_in_never_exceeds_explicit_process_cap(unknown_endpoint, monkeypatch, cap, override, expected, delegated):
    identifier, endpoint = unknown_endpoint
    monkeypatch.setattr(config, 'ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE', True)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', cap)
    record = {'model_profile': identifier}
    if override is not None:
        record['budget_overrides'] = {'MODEL_INPUT_TOKENS': override}
    if delegated:
        monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', False)
        record['delegated_from'] = 'parent'
    assert effective_limits(record, endpoint)['MODEL_INPUT_TOKENS'] == expected


def test_disabled_opt_in_preserves_original_nondefault_limit(unknown_endpoint, monkeypatch):
    identifier, endpoint = unknown_endpoint
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 12000)
    record = {'model_profile': identifier, 'budget_overrides': {'MODEL_INPUT_TOKENS': 40000}}
    assert effective_limits(record, endpoint)['MODEL_INPUT_TOKENS'] == 16000


@pytest.mark.parametrize('invalid', [-1, 1, 2097153, True, 1.5])
def test_opt_in_rejects_invalid_process_cap(unknown_endpoint, monkeypatch, invalid):
    identifier, endpoint = unknown_endpoint
    monkeypatch.setattr(config, 'ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE', True)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', invalid)
    with pytest.raises(ValueError):
        effective_limits({'model_profile': identifier}, endpoint)


def test_runtime_configuration_rejects_out_of_range_authorized_cap(unknown_endpoint, monkeypatch):
    monkeypatch.setattr(config, 'ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE', True)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 2097153)
    with pytest.raises(ValueError, match='MODEL_INPUT_TOKENS'):
        config.validate_runtime_config()


def test_known_capacity_still_limits_input(unknown_endpoint, monkeypatch):
    identifier, endpoint = unknown_endpoint
    monkeypatch.setattr(config, 'ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE', True)
    known = SimpleNamespace(context_tokens=20000, response_reserve_tokens=4096,
                            options=endpoint.options)
    assert effective_limits({'model_profile': identifier}, known)['MODEL_INPUT_TOKENS'] == 15904


def test_opt_in_keeps_provider_request_options_and_absent_max_tokens(unknown_endpoint, monkeypatch):
    from agent_core.core import llm
    identifier, endpoint = unknown_endpoint
    captured = []
    class Stream:
        async def close(self):
            pass
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            captured.append(kwargs)
            return Stream()
        async def close(self):
            pass
    async def client(_endpoint):
        return Client()
    monkeypatch.setattr(llm, '_runtime_client', client)
    async def run():
        for enabled in (False, True):
            monkeypatch.setattr(config, 'ALLOW_UNKNOWN_MODEL_INPUT_OVERRIDE', enabled)
            with model_profile(identifier, endpoint), limits_scope({'model_profile': identifier}, endpoint):
                assert context_budget_metadata()['response_reserve_tokens'] == 4096
                stream = await llm._open_stream([{'role': 'user', 'content': '受控输入'}])
                await stream.close()
    asyncio.run(run())
    assert len(captured) == 2 and captured[0] == captured[1]
    assert captured[0]['model'] == endpoint.model
    assert 'max_tokens' not in captured[0]
