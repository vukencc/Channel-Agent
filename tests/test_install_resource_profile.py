"""Preparation gets only its approved bounded profile; generic commands keep theirs."""
import json

import pytest

from ai_agent_startup import config
from ai_agent_startup.tools import environment, sandbox


def test_prepare_approval_and_execution_use_same_explicit_limits(sandbox_env, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    for name, value in {
        'ENABLE_ENVIRONMENT_PREPARE': True, 'COMMAND_NETWORK': 'allowlist',
        'COMMAND_NETWORK_ALLOWLIST': ['registry.npmjs.org'],
        'ENVIRONMENT_PREPARE_NETWORK_BYTES': 128 * 1024 * 1024,
        'ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS': 32,
        'ENVIRONMENT_PREPARE_NETWORK_TIMEOUT': 30,
        'ENVIRONMENT_PREPARE_FILE_MB': 128, 'ENVIRONMENT_PREPARE_CPU_SECONDS': 30,
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(environment, 'command_memory_limit_mb', lambda profile: 2048)
    plans, launches, executions, proxy_limits = [], [], [], []
    monkeypatch.setattr(environment, 'ask_permission', lambda action, detail, *args, **kw: plans.append(json.loads(detail)) or True)
    monkeypatch.setattr(environment, 'isolated_command', lambda argv, **kw: launches.append(kw) or argv)
    monkeypatch.setattr(environment, '_execute', lambda *args, **kw: executions.append(kw) or '[退出码] 0\n')

    class FakeProxy:
        path = sandbox_env / 'proxy.sock'
        def __init__(self, **kwargs):
            proxy_limits.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def denial_summary(self):
            return ''

    monkeypatch.setattr('ai_agent_startup.tools.network_proxy.NetworkProxy', FakeProxy)
    result = environment.prepare_project_environment('npm', network=True)
    assert '[退出码] 0' in result
    assert plans[0]['network_max_bytes'] == 128 * 1024 * 1024
    assert plans[0]['network_max_connections'] == 32
    assert plans[0]['network_timeout_seconds'] == 30
    assert plans[0]['file_limit_mb'] == 128
    assert plans[0]['cpu_seconds'] == 30
    assert all(row['file_limit_mb'] == 128 and row['cpu_seconds'] == 30 for row in launches)
    assert proxy_limits == [{'max_bytes': 128 * 1024 * 1024, 'max_connections': 32, 'timeout': 30}]
    assert executions[0]['file_limit_mb'] == 128
    assert '--fetch-retries=0' in plans[0]['command']
    assert '--fetch-timeout=30000' in plans[0]['command']
    assert '--maxsockets=2' in plans[0]['command']
    assert '--force' not in plans[0]['command'] and '--legacy-peer-deps' not in plans[0]['command']


def test_prepare_zero_profile_inherits_command_limits(sandbox_env, monkeypatch):
    (sandbox_env / 'requirements.txt').write_text('')
    monkeypatch.setattr(config, 'ENABLE_ENVIRONMENT_PREPARE', True)
    for name in ('NETWORK_BYTES', 'NETWORK_CONNECTIONS', 'NETWORK_TIMEOUT', 'FILE_MB', 'CPU_SECONDS'):
        monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_' + name, 0)
    monkeypatch.setattr(environment, 'isolated_command', lambda argv, **kw: argv)
    plans = []
    monkeypatch.setattr(environment, 'ask_permission', lambda action, detail, *args, **kw: plans.append(json.loads(detail)) or False)
    assert '取消' in environment.prepare_project_environment('uv')
    assert plans[0]['file_limit_mb'] == config.COMMAND_FILE_MB
    assert plans[0]['cpu_seconds'] == config.COMMAND_CPU_SECONDS
    assert plans[0]['network_max_bytes'] == config.COMMAND_NETWORK_MAX_BYTES


@pytest.mark.parametrize('kwargs', [{'file_limit_mb': 0}, {'file_limit_mb': 1025},
                                  {'file_limit_mb': True}, {'cpu_seconds': 0},
                                  {'cpu_seconds': 3601}, {'cpu_seconds': float('inf')}])
def test_sandbox_rejects_invalid_internal_resource_overrides(sandbox_env, kwargs):
    with pytest.raises(sandbox.SandboxError):
        sandbox.isolated_command(['/bin/true'], **kwargs)


def test_sandbox_override_does_not_change_next_generic_launch(sandbox_env):
    custom = sandbox.isolated_command(['/bin/true'], file_limit_mb=128, cpu_seconds=30)
    generic = sandbox.isolated_command(['/bin/true'])
    assert '--fsize=134217728' in custom and '--cpu=30' in custom
    assert f'--fsize={config.COMMAND_FILE_MB * 1024 * 1024}' in generic
    assert f'--cpu={config.COMMAND_CPU_SECONDS}' in generic


def test_full_access_still_audits_exact_approved_prepare_profile(sandbox_env, monkeypatch):
    (sandbox_env / 'requirements.txt').write_text('')
    monkeypatch.setattr(config, 'ENABLE_ENVIRONMENT_PREPARE', True)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_FILE_MB', 128)
    monkeypatch.setattr(config, 'ENVIRONMENT_PREPARE_CPU_SECONDS', 30)
    monkeypatch.setattr(environment, 'isolated_command', lambda argv, **kw: argv)
    monkeypatch.setattr(environment, '_execute', lambda *args, **kw: '[退出码] 0\n')
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: pytest.fail('full_access should be automatic'))
    assert '[退出码] 0' in environment.prepare_project_environment('uv')
    rows = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    confirmation = next(row for row in rows if row.get('event') == 'confirm')
    assert confirmation['allowed'] and confirmation['decision_source'] == 'full_access'
    plan = json.loads(confirmation['detail'])
    assert plan['file_limit_mb'] == 128 and plan['cpu_seconds'] == 30
    started = next(row for row in rows if row.get('event') == 'environment_prepare_started')
    assert all(started[key] == plan[key] for key in (
        'file_limit_mb', 'cpu_seconds', 'network_max_bytes', 'network_max_connections',
        'network_timeout_seconds', 'timeout_seconds'))


def test_file_size_error_keeps_stderr_and_reports_exact_active_limit(sandbox_env, monkeypatch):
    import shlex
    from ai_agent_startup.tools.command import _execute
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 20000)
    command = 'printf %s ' + shlex.quote('npm error code EFBIG\nnpm error file too large\n') + ' >&2; exit 1'
    argv = ['/bin/sh', '-c', command]
    # The harmless fixture emits the real package-manager message; no resource override
    # is used to execute a host installation or modify user data.
    isolated = sandbox.isolated_command(argv, file_limit_mb=128)
    result = _execute(command, '', argv, isolated, file_limit_mb=128)
    assert 'npm error code EFBIG\nnpm error file too large' in result
    assert '单个文件大小上限为 128 MiB' in result
    assert '[退出码] 1' in result
