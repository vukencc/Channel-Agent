"""运行时档位的确认、诊断与 fail-closed 回归。"""
import json

from ai_agent_startup import config
from ai_agent_startup.tools import command, sandbox


def test_node_profile_requires_explicit_binding_before_confirmation(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 0)
    approvals = []
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: approvals.append(args) or True)
    result = command.run_command('touch not-executed', runtime_profile='node')
    assert '[已拦截]' in result
    assert not approvals
    assert not (sandbox_env / 'not-executed').exists()


def test_node_profile_quota_visible_in_confirmation(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': '/trusted/node'})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048)
    profiles, approvals = [], []
    monkeypatch.setattr(command, 'isolated_command', lambda argv, **kwargs: profiles.append(kwargs) or argv)
    monkeypatch.setattr(command, '_execute', lambda *args, **kwargs: '[退出码] 0\n[stdout]\n3')
    monkeypatch.setattr(sandbox, 'confirmer', lambda prompt, timeout: approvals.append(prompt) or True)
    assert '[退出码] 0' in command.run_command('node -e "console.log(3)"', runtime_profile='node')
    assert profiles == [{'runtime_profile': 'node'}]
    assert 'node' in approvals[0] and '2048 MiB' in approvals[0]


def test_missing_sandbox_binary_does_not_claim_host_uninstalled(sandbox_env):
    result = command.run_command('nonexistent_agent_runtime_xyz')
    assert '[退出码] 127' in result
    assert '不代表宿主未安装' in result and 'inspect_environment' in result
    records = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    assert any(row['event'] == 'executed' and row['exit_code'] == 127 for row in records)
