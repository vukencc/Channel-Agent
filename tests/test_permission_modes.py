import json
import os
import subprocess
import sys
from threading import Event

import pytest

from ai_agent_startup import config
from ai_agent_startup.core import permissions
from ai_agent_startup.tools import file_crud, sandbox


@pytest.mark.parametrize('policy', ['smart', 'full_access'])
def test_new_permission_modes_are_valid(sandbox_env, monkeypatch, policy):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    assert sandbox.current_policy() == policy


def test_smart_creates_and_appends_without_confirmation(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'smart')
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: pytest.fail('safe write requested confirmation'))
    assert '完成' in file_crud.create_file('notes/one.txt', 'first')
    assert '完成' in file_crud.append_file('notes/one.txt', ' second', 5)
    assert (sandbox_env / 'notes/one.txt').read_text() == 'first second'
    events = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    assert len([row for row in events if row.get('decision_source') == 'smart_safe']) == 2


@pytest.mark.parametrize('action', ['delete_file', 'move', 'update_file', 'edit_file', 'web_search', 'unknown'])
def test_smart_risky_operations_still_confirm(sandbox_env, monkeypatch, action):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'smart')
    prompts = []
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: prompts.append(args) or False)
    assert not sandbox.ask_permission(action, 'existing.txt', paths=['existing.txt'])
    assert len(prompts) == 1


@pytest.mark.parametrize('command', ['pwd', 'ls -la', 'head -n 10 notes.txt', 'cat -- notes.txt', 'wc -l notes.txt'])
def test_smart_simple_read_commands_are_low_risk(sandbox_env, command):
    (sandbox_env / 'notes.txt').write_text('notes')
    risk = permissions.evaluate_operation_risk('run_command', command, command=command)
    assert risk.level == 'low'
    assert risk.requires_confirmation is False


@pytest.mark.parametrize('command', [
    'ls; rm notes.txt', 'ls && cat notes.txt', 'ls | head', 'ls > output.txt',
    'ls "$(touch bad)"', 'ls `touch bad`', 'ls\ntouch bad', 'sh -c ls',
    'curl https://example.com', 'python3 -c "print(1)"', 'find . -delete',
    'head /etc/passwd', 'cat ../outside', 'ls *', 'ls --unrecognized',
    'head --bytes=1 /proc/self/environ',
])
def test_smart_ambiguous_or_risky_commands_confirm(sandbox_env, monkeypatch, command):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'smart')
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    assert not sandbox.ask_permission('run_command', command, command=command)


def test_smart_force_confirmation_cannot_be_downgraded(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'smart')
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    assert not sandbox.ask_permission('run_command', 'pwd', command='pwd', force_confirmation=True)


def test_smart_new_file_requires_verified_path(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'smart')
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    (sandbox_env / 'existing.txt').write_text('preserve')
    for paths in (None, ['existing.txt'], ['../outside'], ['/tmp/outside'], ['@unknown/file']):
        assert not sandbox.ask_permission('create_file', 'safe according to model', paths=paths)


@pytest.mark.parametrize('policy', ['smart', 'full_access'])
def test_permission_modes_preserve_path_boundary(sandbox_env, monkeypatch, policy):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    assert '拦截' in file_crud.create_file('../outside.txt', 'blocked')
    assert not (sandbox_env.parent / 'outside.txt').exists()


def test_full_access_bypasses_even_forced_confirmation_and_audits(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: pytest.fail('full access requested confirmation'))
    assert sandbox.ask_permission('unknown', 'explicit full access', force_confirmation=True)
    assert '完成' in file_crud.create_file('existing.txt', 'first')
    assert '完成' in file_crud.update_file('existing.txt', 'second')
    assert '完成' in file_crud.delete_file('existing.txt')
    events = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    confirmations = [row for row in events if row['event'] == 'confirm']
    assert len(confirmations) == 4
    assert all(row['decision_source'] == 'full_access' and row['allowed'] for row in confirmations)


@pytest.mark.parametrize('policy', ['smart', 'full_access'])
def test_auto_permission_does_not_override_cancellation(sandbox_env, monkeypatch, policy):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    cancelled = Event()
    cancelled.set()
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG,
                                  lambda *_: pytest.fail('cancelled request confirmed'), cancelled)
    with sandbox.tool_context(context):
        assert not sandbox.ask_permission('create_file', 'new.txt', paths=['new.txt'])


def test_smart_does_not_allow_read_command_through_escaping_symlink(sandbox_env):
    (sandbox_env / 'outside').symlink_to(sandbox_env.parent, target_is_directory=True)
    risk = permissions.evaluate_operation_risk('run_command', command='ls outside')
    assert risk.requires_confirmation is True


@pytest.mark.parametrize('policy', ['smart', 'full_access'])
def test_new_modes_are_accepted_by_environment_configuration(policy):
    result = subprocess.run(
        [sys.executable, '-c', 'from ai_agent_startup import config; print(config.TOOL_PERMISSION_POLICY)'],
        env={**os.environ, 'TOOL_PERMISSION_POLICY': policy},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == policy


@pytest.mark.parametrize('policy', ['smart', 'full_access'])
def test_modes_still_fail_closed_when_command_isolation_is_unavailable(sandbox_env, monkeypatch, policy):
    import shutil
    from ai_agent_startup.tools.command import run_command
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: None)
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: pytest.fail('blocked command requested confirmation'))
    assert '拦截' in run_command('touch no.txt')
    assert not (sandbox_env / 'no.txt').exists()


@pytest.mark.parametrize('policy', ['smart', 'full_access'])
def test_modes_do_not_bypass_command_workspace_quota(sandbox_env, monkeypatch, policy):
    from ai_agent_startup.tools.command import run_command
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    (sandbox_env / 'existing.txt').write_text('over quota')
    assert '配额' in run_command('pwd')


@pytest.mark.parametrize('policy', ['smart', 'full_access'])
def test_modes_do_not_write_to_readonly_roots(sandbox_env, tmp_path, monkeypatch, policy):
    readonly = tmp_path / 'readonly'
    readonly.mkdir()
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    monkeypatch.setattr(config, 'TOOL_ROOTS', {'docs': {'path': str(readonly), 'read_only': True}})
    assert '拦截' in file_crud.create_file('@docs/no.txt', 'no')
    assert not (readonly / 'no.txt').exists()


def test_new_modes_keep_context_policy_isolated(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: False,
                                  Event(), permission_policy='smart')
    with sandbox.tool_context(context):
        assert sandbox.current_policy() == 'smart'
        assert not sandbox.ask_permission('delete_file', 'existing.txt')
    assert sandbox.current_policy() == 'full_access'
