"""固定共享工作区，不再提供用户或模型可选的工作区模式。"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document
from pydantic import ValidationError

from ai_agent_startup.core.cli_commands import COMMANDS, CommandCompleter
from ai_agent_startup.tools.session_tools import CreateSessionArgs


def test_model_cannot_select_workspace_mode():
    assert 'workspace_mode' not in CreateSessionArgs.model_fields
    for mode in ('shared', 'isolated'):
        with pytest.raises(ValidationError):
            CreateSessionArgs(task='inspect', workspace_mode=mode)


def test_cli_does_not_offer_workspace_mode_commands():
    assert '/workspace' not in {command.name for command in COMMANDS}
    completer = CommandCompleter(SimpleNamespace())
    assert not list(completer.get_completions(Document('/workspace '), None))


def test_web_forms_do_not_offer_workspace_mode():
    assets = Path(__file__).parents[1] / 'src/ai_agent_startup/web/static'
    html, script = (assets / 'index.html').read_text(), (assets / 'app.js').read_text()
    for identifier in ('new-workspace', 'settings-workspace'):
        assert identifier not in html
        assert identifier not in script


def test_web_requests_cannot_select_workspace_mode():
    pytest.importorskip('fastapi')
    from ai_agent_startup.web.app import NewSession, Settings
    for model in (NewSession, Settings):
        assert 'workspace_mode' not in model.model_fields
        for mode in ('shared', 'isolated'):
            with pytest.raises(ValidationError):
                model(workspace_mode=mode)
