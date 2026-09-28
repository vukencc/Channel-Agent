import asyncio

import pytest

import config
from core import llm


def test_sampling_and_tool_choice_are_sent_only_when_configured(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_PARAMETERS', {'temperature': 0.2, 'top_p': 0.8,
        'max_tokens': 300, 'parallel_tool_calls': False, 'tool_choice': 'none'}, raising=False)
    options = llm.model_options()
    assert options['temperature'] == 0.2
    assert options['tool_choice'] == 'none'
    assert options['parallel_tool_calls'] is False


def test_profiles_are_context_local(monkeypatch):
    from core.model_settings import model_profile, active_profile
    async def run(name):
        with model_profile(name):
            await asyncio.sleep(0)
            assert active_profile.get() == name
    async def both():
        await asyncio.gather(run('a'), run('b'))
    asyncio.run(both())
    assert active_profile.get() is None


@pytest.mark.parametrize('value', [{'temperature': -1}, {'top_p': 2}, {'max_tokens': 0},
                                  {'extra_body': {'unsafe': True}}, {'tool_choice': 'unknown'}])
def test_invalid_model_parameters_rejected(value):
    from core.model_settings import ModelParameters
    with pytest.raises(ValueError):
        ModelParameters.model_validate(value)


def test_profile_is_persisted_and_request_parameters_reach_sdk(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.sessions import SessionManager
    from core.storage import SessionStore
    captured = []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            captured.append(kwargs)
    monkeypatch.setattr(llm, 'get_client', lambda: Client())
    monkeypatch.setattr(config, 'MODEL_PROFILES', {'precise': {'model': 'profile-model',
        'parameters': {'temperature': 0.1, 'tool_choice': 'none'}}})
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        manager = SessionManager(store, model=llm._open_stream)
        session = manager.create()
        manager.set_model_profile(session, 'precise')
        async def run():
            await manager._call_model([], session_id=session.id)
        asyncio.run(run())
        restored = store.load_all()[0]
        assert restored['model_profile'] == 'precise'
        assert captured[0]['model'] == 'profile-model'
        assert captured[0]['temperature'] == 0.1
        assert captured[0]['tool_choice'] == 'none'
    finally:
        store.close()
