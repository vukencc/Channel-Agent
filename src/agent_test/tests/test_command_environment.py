"""运行时档位的确认、诊断与 fail-closed 回归。"""
import json

import pytest
from pydantic import ValidationError

from agent_core import config
from agent_core.tools import TOOL_REGISTRY, command, network_proxy, sandbox


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


def _record_command(monkeypatch):
    isolated, executed, approvals = [], [], []
    monkeypatch.setattr(command, 'isolated_command', lambda argv, **kwargs: isolated.append(kwargs) or argv)
    monkeypatch.setattr(command, '_execute', lambda *args, **kwargs: executed.append(kwargs) or '[退出码] 0\n')
    monkeypatch.setattr(command, 'ask_permission', lambda *args, **kwargs: approvals.append((args, kwargs)) or True)
    return isolated, executed, approvals


def test_resource_profile_schema_rejects_unknown_profile():
    with pytest.raises(ValidationError):
        TOOL_REGISTRY['run_command'].run('{"command":"true","resource_profile":"unlimited"}')


def test_standard_profile_keeps_generic_limits(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_CPU_SECONDS', 100)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_FILE_MB', 256)
    isolated, executed, approvals = _record_command(monkeypatch)
    assert '[退出码] 0' in command.run_command('printf standard')
    assert isolated == [{'runtime_profile': 'system'}]
    assert executed == [{}]
    assert approvals[0][1]['force_confirmation'] is False


def test_development_profile_shares_configured_prepare_limits_and_discloses_plan(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_TIMEOUT', 180)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_CPU_SECONDS', 90)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_FILE_MB', 256)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_NETWORK_BYTES', 128 * 1024 * 1024)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS', 32)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_NETWORK_TIMEOUT', 30)
    isolated, executed, approvals = _record_command(monkeypatch)
    assert '[退出码] 0' in command.run_command('printf build', resource_profile='development')
    assert isolated == [{'runtime_profile': 'system', 'file_limit_mb': 256, 'cpu_seconds': 90}]
    assert executed == [{'timeout': 180, 'file_limit_mb': 256}]
    plan = json.loads(approvals[0][0][1])
    assert plan['resource_profile'] == 'development'
    assert plan['timeout_seconds'] == 180
    assert plan['network_max_bytes'] == 128 * 1024 * 1024
    assert plan['network_max_connections'] == 32
    assert plan['network_timeout_seconds'] == 30
    assert plan['memory_limit_mb'] == config.COMMAND_MEMORY_MB
    assert plan['workspace_limit_mb'] == config.WORKSPACE_LIMIT_MB
    assert plan['network'] is False and plan['domains'] == []
    assert approvals[0][1]['force_confirmation'] is True


def test_development_zero_limits_inherit_generic_bounds_and_node_memory(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': '/trusted/node'})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048)
    for name in ('NETWORK_BYTES', 'NETWORK_CONNECTIONS', 'NETWORK_TIMEOUT', 'FILE_MB', 'CPU_SECONDS'):
        monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_' + name, 0)
    isolated, executed, approvals = _record_command(monkeypatch)
    assert '[退出码] 0' in command.run_command('npm test', runtime_profile='node', resource_profile='development')
    assert isolated == [{'runtime_profile': 'node', 'file_limit_mb': config.COMMAND_FILE_MB,
                         'cpu_seconds': config.COMMAND_CPU_SECONDS}]
    plan = json.loads(approvals[0][0][1])
    assert plan['memory_limit_mb'] == 2048
    assert plan['network_max_bytes'] == config.COMMAND_NETWORK_MAX_BYTES
    assert plan['network_max_connections'] == config.COMMAND_NETWORK_MAX_CONNECTIONS
    assert plan['network_timeout_seconds'] == config.COMMAND_NETWORK_TIMEOUT


def test_development_network_passes_exact_approved_limits(sandbox_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'allowlist')
    monkeypatch.setattr(config, 'COMMAND_NETWORK_ALLOWLIST', ['example.com'])
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_NETWORK_BYTES', 1024)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS', 12)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_NETWORK_TIMEOUT', 20)
    isolated, executed, approvals = _record_command(monkeypatch)
    proxies = []

    class Proxy:
        path = tmp_path / 'proxy.sock'
        def __init__(self, **kwargs):
            proxies.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(network_proxy, 'NetworkProxy', Proxy)
    assert '[退出码] 0' in command.run_command('printf network', network=True, resource_profile='development')
    assert proxies == [{'max_bytes': 1024, 'max_connections': 12, 'timeout': 20}]
    assert isolated[1]['network_socket'] == Proxy.path
    assert isolated[1]['file_limit_mb'] == config.COMMAND_FILE_MB
    plan = json.loads(approvals[0][0][1])
    assert plan['network'] is True and plan['domains'] == ['example.com']


def test_development_profile_cannot_bypass_confirmation(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'trusted')
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [
        {'tool': 'run_command', 'path_prefix': None, 'command_prefix': ['printf']}])
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    executed = []
    monkeypatch.setattr(command, '_execute', lambda *args, **kwargs: executed.append(args) or 'unexpected')
    assert '取消' in command.run_command('printf denied', resource_profile='development')
    assert executed == []


def test_unknown_resource_profile_rejected_before_confirmation(sandbox_env, monkeypatch):
    approvals = []
    monkeypatch.setattr(command, 'ask_permission', lambda *args, **kwargs: approvals.append(args) or True)
    assert '拦截' in command.run_command('touch never', resource_profile='unlimited')
    assert approvals == [] and not (sandbox_env / 'never').exists()


def test_development_profile_applies_real_sandbox_rlimits(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_CPU_SECONDS', 20)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_FILE_MB', 64)
    result = command.run_command(
        'python3 -c "import resource; print(resource.getrlimit(resource.RLIMIT_CPU)[0], '
        'resource.getrlimit(resource.RLIMIT_FSIZE)[0])"', resource_profile='development')
    assert '[退出码] 0' in result, result
    assert f'20 {64 * 1024 * 1024}' in result
