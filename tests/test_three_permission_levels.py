"""The public three-mode contract preserves conservative legacy enforcement."""
import threading

import pytest

from ai_agent_startup.core import permissions
from ai_agent_startup.core.cli_commands import POLICY_LABELS
from ai_agent_startup.tools.sandbox import ToolContext, ask_permission, tool_context


def test_public_modes_are_exactly_three_with_chinese_labels():
    assert permissions.PERMISSION_POLICIES == {'full_access', 'smart', 'readonly'}
    assert POLICY_LABELS == {'full_access': '完全访问', 'smart': '智能申请', 'readonly': '只读'}


@pytest.mark.parametrize('stored', ['standard', 'trusted'])
def test_legacy_mode_metadata_is_canonical_without_automatic_permission(
        tmp_path, sandbox_env, monkeypatch, stored):
    from ai_agent_startup import config
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [])
    assert permissions.public_permission_policy(stored) == 'smart'
    calls = []
    context = ToolContext(sandbox_env, tmp_path / 'audit.jsonl',
                          lambda *_: calls.append('asked') or False, threading.Event(),
                          permission_policy=stored)
    with tool_context(context):
        assert not ask_permission('create_file', 'new file', paths=['new.txt'])
    assert calls == ['asked']


@pytest.mark.parametrize('mode,allowed,asked', [
    ('full_access', True, False), ('smart', False, True), ('readonly', False, False),
])
def test_three_modes_apply_distinct_risky_write_decisions(
        tmp_path, sandbox_env, mode, allowed, asked):
    calls = []
    context = ToolContext(sandbox_env, tmp_path / 'audit.jsonl',
                          lambda *_: calls.append('asked') or False, threading.Event(),
                          permission_policy=mode)
    with tool_context(context):
        assert ask_permission('delete_file', 'existing file', paths=['existing.txt']) is allowed
    assert bool(calls) is asked
