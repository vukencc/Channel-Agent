import json

import pytest

from ai_agent_startup import config
from ai_agent_startup.tools import file_crud, sandbox
from ai_agent_startup.tools.command import run_command


def test_readonly_policy_denies_writes_even_with_affirmative_confirmer(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'readonly', raising=False)
    result = file_crud.create_file('file.txt', '正文')
    assert '完成' not in result
    assert not (sandbox_env / 'file.txt').exists()
    result = run_command('touch command.txt')
    assert '[退出码] 0' not in result
    assert not (sandbox_env / 'command.txt').exists()


def test_trusted_path_rule_is_component_scoped_and_audited(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'trusted', raising=False)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [
        {'tool': 'create_file', 'path_prefix': 'notes', 'command_prefix': None}], raising=False)
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    assert '完成' in file_crud.create_file('notes/one.txt', '批准的写入')
    assert '取消' in file_crud.create_file('notes-other/no.txt', '不匹配')
    assert '拦截' in file_crud.create_file('../escape.txt', '越界')
    events = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    assert any(row.get('decision_source') == 'trusted_rule' and row['allowed'] for row in events)


def test_command_prefix_does_not_approve_shell_composition(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'trusted', raising=False)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [
        {'tool': 'run_command', 'path_prefix': None, 'command_prefix': ['printf']}], raising=False)
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    assert '[退出码] 0' in run_command('printf allowed')
    for command in ('printfx bad', 'printf allowed; touch bad', 'printf "$(touch bad)"',
                    'printf allowed > bad', 'printf `touch bad`'):
        assert '取消' in run_command(command)
    assert not (sandbox_env / 'bad').exists()


def test_standard_ignores_trusted_rules(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'standard', raising=False)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [
        {'tool': 'create_file', 'path_prefix': '.', 'command_prefix': None}], raising=False)
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    assert '取消' in file_crud.create_file('no.txt', '正文')


def test_policy_persists_per_session(tmp_path):
    from ai_agent_startup.core.sessions import SessionManager
    from ai_agent_startup.core.storage import SessionStore
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        manager = SessionManager(store)
        session = manager.create()
        manager.set_permission_policy(session, 'readonly')
        assert store.load_all()[0]['permission_policy'] == 'readonly'
        with pytest.raises(ValueError):
            manager.set_permission_policy(session, 'unknown')
    finally:
        store.close()


def test_concurrent_sessions_keep_policy_isolated(tmp_path, monkeypatch):
    import asyncio
    from ai_agent_startup.core.sessions import SessionManager
    from ai_agent_startup.core.storage import SessionStore
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'standard')
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [
        {'tool': 'create_file', 'path_prefix': '.', 'command_prefix': None}])
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        first, second = manager.create(), manager.create()
        manager.set_permission_policy(first, 'readonly')
        manager.set_permission_policy(second, 'trusted')
        call = {'id': 'write', 'function': {'name': 'create_file', 'arguments': '{"path":"file.txt"}'}}
        try:
            results = await asyncio.gather(manager._tool(first, call), manager._tool(second, call))
            assert '拦截' in results[0] and '完成' in results[1]
            assert not (store.workspace_path(first.id) / 'file.txt').exists()
            assert (store.workspace_path(second.id) / 'file.txt').exists()
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())
