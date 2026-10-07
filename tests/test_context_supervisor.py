"""Supervisor configuration stays separate from the primary model account."""

import os
import asyncio
import json
from types import SimpleNamespace

import pytest


def test_supervisor_settings_load_prefixed_file_without_mutating_process_environment(tmp_path, monkeypatch):
    from ai_agent_startup.core.context_supervisor import SupervisorSettings

    env_file = tmp_path / '.env.supervisor'
    env_file.write_text(
        'SUPERVISOR_ENABLED=true\n'
        'SUPERVISOR_API_KEY=file-secret\n'
        'SUPERVISOR_BASE_URL=https://example.org/v1\n'
        'SUPERVISOR_MODEL=file-model\n'
        'SUPERVISOR_TIMEOUT=8\n', encoding='utf-8')
    monkeypatch.setenv('SUPERVISOR_MODEL', 'process-model')
    before = dict(os.environ)
    settings = SupervisorSettings.from_env_file(env_file)
    assert settings.enabled is True
    assert settings.model == 'process-model'
    assert settings.api_key == 'file-secret'
    assert settings.base_url == 'https://example.org/v1'
    assert settings.timeout == 8
    assert dict(os.environ) == before


def test_supervisor_does_not_inherit_primary_model_credentials(tmp_path, monkeypatch):
    from ai_agent_startup.core.context_supervisor import SupervisorSettings

    env_file = tmp_path / '.env.supervisor'
    env_file.write_text('SUPERVISOR_ENABLED=false\n', encoding='utf-8')
    monkeypatch.setenv('API_KEY', 'primary-secret')
    monkeypatch.setenv('BASE_URL', 'https://primary.example/v1')
    monkeypatch.delenv('SUPERVISOR_API_KEY', raising=False)
    monkeypatch.delenv('SUPERVISOR_BASE_URL', raising=False)
    settings = SupervisorSettings.from_env_file(env_file)
    assert settings.api_key == ''
    assert settings.base_url == ''


@pytest.mark.parametrize('base_url', ['file:///tmp/model', 'http://example.org:70000/v1',
                                     'https://example.org/\x01v1'])
def test_enabled_supervisor_rejects_unsafe_endpoint(tmp_path, monkeypatch, base_url):
    from ai_agent_startup.core.context_supervisor import SupervisorSettings

    env_file = tmp_path / '.env.supervisor'
    env_file.write_text('SUPERVISOR_ENABLED=true\nSUPERVISOR_API_KEY=key\n'
                        f'SUPERVISOR_BASE_URL={base_url}\nSUPERVISOR_MODEL=judge\n', encoding='utf-8')
    for key in ('SUPERVISOR_ENABLED', 'SUPERVISOR_API_KEY', 'SUPERVISOR_BASE_URL', 'SUPERVISOR_MODEL'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        SupervisorSettings.from_env_file(env_file)


def test_supervisor_model_calls_have_no_tools_or_stream_and_validate_scores():
    from ai_agent_startup.core.context_supervisor import SupervisorAgent, SupervisorSettings

    class Completions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            answer = {'compact': True, 'reason': 'older material', 'values': [0.4]}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(answer), tool_calls=None))], usage=None)

    async def run():
        completions = Completions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        settings = SupervisorSettings(enabled=True, api_key='test', base_url='https://example.org/v1', model='judge')
        supervisor = SupervisorAgent(settings=settings, client=client)
        decision = await supervisor.inspect({'chunks': [{'ID': 'a' * 32}]})
        assert decision == {'compact': True, 'reason': 'older material', 'values': [0.4]}
        assert completions.calls and completions.calls[0]['tools'] == []
        assert completions.calls[0]['tool_choice'] == 'none'
        assert completions.calls[0]['stream'] is False

    asyncio.run(run())
