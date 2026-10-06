"""Agent inbox messages remain reference material, not new user requests."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ai_agent_startup import config
from ai_agent_startup.core import llm
from ai_agent_startup.core.cli import AgentCLI
from ai_agent_startup.core.context import ContextBudgetError, build_model_history
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.core.transcript import Transcript


@pytest.fixture
def store(tmp_path):
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


def _agent_progress():
    return {'role': 'user', 'content': '[Agent 通信] progress',
            '_agent_message': {'sender_id': 'a' * 32, 'seq': 1}}


def test_cli_retry_selects_last_human_user_not_agent_inbox_message(store, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BRANCHES', True)
    with create_pipe_input() as pipe:
        cli = AgentCLI(store, input=pipe, output=DummyOutput())
        session = cli.active
        session.record['messages'].extend([
            {'role': 'user', 'content': 'real user task'},
            {'role': 'assistant', 'content': 'working'},
            _agent_progress(),
            {'role': 'assistant', 'content': 'latest answer'},
        ])
        selected = []

        async def capture_resend(parent, index, text):
            selected.append((parent.id, index, text))
            return parent

        monkeypatch.setattr(cli.manager, 'resend', capture_resend)
        assert cli.handle('/retry') is True
        assert selected == [(session.id, 1, None)]


def test_resend_rejects_agent_inbox_user_even_with_replacement_text(store, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BRANCHES', True)

    async def run():
        manager = SessionManager(store)
        parent = manager.create()
        parent.record['messages'].extend([
            {'role': 'user', 'content': 'real user task'},
            {'role': 'assistant', 'content': 'working'},
            _agent_progress(),
        ])
        await manager.save(parent)
        for replacement in (None, 'try replacing message'):
            with pytest.raises(ValueError):
                await manager.resend(parent, 3, replacement)
        assert list(manager.sessions) == [parent.id]
        await manager.shutdown()

    asyncio.run(run())


def test_agent_progress_cannot_evict_oversized_human_request(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 512)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 262144)
    messages = [
        {'role': 'system', 'content': 'Follow the actual user task.'},
        {'role': 'user', 'content': 'real task ' + 'x' * 1200},
        {'role': 'assistant', 'content': 'working'},
        _agent_progress(),
    ]
    with pytest.raises(ContextBudgetError):
        build_model_history(messages)


def test_model_wire_omits_agent_message_marker_without_mutating_history(monkeypatch):
    captured = []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **kwargs):
            return self

        async def create(self, **kwargs):
            captured.append(kwargs)
            return object()

    monkeypatch.setattr(llm, 'get_client', lambda: Client())
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [])
    history = [{'role': 'system', 'content': 'system'}, _agent_progress()]
    original = deepcopy(history)
    asyncio.run(llm._open_stream(history, 'agent-session'))
    assert captured
    assert '_agent_message' not in captured[0]['messages'][1]
    assert captured[0]['messages'][1]['content'] == history[1]['content']
    assert history == original


def test_transcript_labels_agent_inbox_message_with_sender():
    transcript = Transcript()
    transcript.sync([{'role': 'system', 'content': 'system'}, _agent_progress()])
    labels = [line for line in transcript.lines if line.startswith('━━')]
    assert any('Agent 来信' in label and 'aaaaaaaa' in label for label in labels)
    assert all('━━ 你' not in label for label in labels)
