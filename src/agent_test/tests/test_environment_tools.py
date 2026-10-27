"""Environment inspection and preparation stay inside the tool sandbox."""

import json

import pytest
from pydantic import ValidationError

from agent_core import config
from agent_core.tools import TOOL_REGISTRY, sandbox
from agent_core.tools import environment


@pytest.fixture
def enabled_prepare(monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_ENVIRONMENT_PREPARE', True, raising=False)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'smart')
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': '/opt/test-node'}, raising=False)
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048, raising=False)
    # Command assembly and approval are under test here; no host runtime is needed.
    monkeypatch.setattr(environment, 'isolated_command', lambda argv, **kwargs: argv)
    monkeypatch.setattr(environment, 'command_memory_limit_mb', lambda profile: 2048)


def _record_execution(monkeypatch, result='[退出码] 0\n[stdout]\ninstalled'):
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(environment, '_execute', execute)
    return calls


def _command_text(calls):
    assert len(calls) == 1
    args, kwargs = calls[0]
    return ' '.join(str(value) for value in (*args, *kwargs.values()))


def test_environment_tools_always_registered():
    assert TOOL_REGISTRY['inspect_environment'].fn is environment.inspect_environment
    assert TOOL_REGISTRY['prepare_project_environment'].fn is environment.prepare_project_environment


def test_npm_project_config_cannot_switch_install_to_sibling(sandbox_env, enabled_prepare, monkeypatch):
    from pathlib import Path
    prefix = Path.home() / '.nvm/versions/node/v24.21.0'
    if not (prefix / 'bin/node').is_file():
        pytest.skip('需要本地 Linux Node 发行版验证真实 npm 配置边界')
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(prefix)})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048)
    monkeypatch.setattr(environment, 'isolated_command', sandbox.isolated_command)
    project, victim = sandbox_env / 'project', sandbox_env / 'victim'
    project.mkdir()
    victim.mkdir()
    (project / 'package.json').write_text('{"name":"boundary-fixture","version":"1.0.0"}')
    (project / '.npmrc').write_text('global=true\nprefix=/workspace/victim\n')
    result = environment.prepare_project_environment('npm', project_dir='project')
    assert '[退出码] 0' in result, result
    assert not list(victim.iterdir())


def test_prepare_rejects_unsupported_manager_at_tool_boundary():
    with pytest.raises(ValidationError):
        TOOL_REGISTRY['prepare_project_environment'].run('{"manager": "pip"}')


def test_inspection_reports_runtime_statuses_as_json(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_ENVIRONMENT_PREPARE', False, raising=False)
    result = json.loads(environment.inspect_environment())
    assert set(result['runtimes']) == {'node', 'npm', 'python', 'uv', 'java', 'maven'}
    assert all(item['status'] in {'available', 'missing', 'resource_limited', 'blocked'}
               for item in result['runtimes'].values())


def test_node_inspection_probes_real_execution_with_explicit_profile(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': '/opt/node'}, raising=False)
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 512, raising=False)
    calls = []

    def isolated(argv, **kwargs):
        calls.append((argv, kwargs))
        raise sandbox.SandboxError('probe stopped')

    monkeypatch.setattr(environment, 'isolated_command', isolated)
    result = json.loads(environment.inspect_environment())
    assert any('node' in str(argv) and '-e' in str(argv)
               and kwargs.get('runtime_profile') == 'node'
               for argv, kwargs in calls)
    assert result['runtimes']['node']['status'] in {'blocked', 'resource_limited'}


def test_inspection_ignores_workspace_json_module_and_never_confirms(sandbox_env, monkeypatch):
    (sandbox_env / 'json.py').write_text(
        'open("inspection-sentinel", "w").write("executed")\n'
    )
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: False)
    result = json.loads(environment.inspect_environment())
    assert result['runtimes']['python']['status'] == 'available'
    assert not (sandbox_env / 'inspection-sentinel').exists()


def test_prepare_disabled_rejects_before_execution(sandbox_env, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    monkeypatch.setattr(config, 'ENABLE_ENVIRONMENT_PREPARE', False, raising=False)
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('npm')
    assert '拦截' in result or '禁用' in result or 'disabled' in result.lower()
    assert calls == []


@pytest.mark.parametrize('path', ['../outside', '/tmp/outside'])
def test_prepare_rejects_paths_outside_sandbox(sandbox_env, enabled_prepare, monkeypatch, path):
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('npm', project_dir=path)
    assert '拦截' in result or '路径' in result
    assert calls == []


@pytest.mark.parametrize('manager,manifest', [('npm', 'package.json'), ('uv', 'requirements.txt')])
def test_prepare_requires_project_manifest(sandbox_env, enabled_prepare, monkeypatch, manager, manifest):
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment(manager)
    assert manifest in result
    assert calls == []


def test_prepare_uv_project_requires_lock(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'pyproject.toml').write_text('[project]\nname="example"\nversion="0.1"\n')
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('uv')
    assert 'uv.lock' in result
    assert calls == []


def test_prepare_rejects_symlink_escape(sandbox_env, enabled_prepare, monkeypatch):
    outside = sandbox_env.parent / 'outside'
    outside.mkdir()
    (outside / 'package.json').write_text('{}')
    (sandbox_env / 'escape').symlink_to(outside, target_is_directory=True)
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('npm', project_dir='escape')
    assert '拦截' in result or '路径' in result
    assert calls == []


def test_prepare_rejects_dependency_directory_symlink_without_writing_victim(
        sandbox_env, enabled_prepare, monkeypatch):
    victim = sandbox_env / 'victim'
    victim.mkdir()
    (victim / 'keep.txt').write_text('safe')
    (sandbox_env / 'requirements.txt').write_text('')
    (sandbox_env / '.agent-env').symlink_to(victim, target_is_directory=True)
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('uv')
    assert '拦截' in result
    assert calls == []
    assert sorted(path.name for path in victim.iterdir()) == ['keep.txt']


def test_prepare_guard_ignores_workspace_json_and_pathlib_modules(
        sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'requirements.txt').write_text('')
    for module in ('json', 'pathlib'):
        (sandbox_env / f'{module}.py').write_text(
            f'open("{module}-sentinel", "w").write("executed")\n'
        )
    monkeypatch.setattr(environment, 'isolated_command', sandbox.isolated_command)
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {})
    result = environment.prepare_project_environment('uv')
    assert '[退出码]' in result
    assert not (sandbox_env / 'json-sentinel').exists()
    assert not (sandbox_env / 'pathlib-sentinel').exists()


def test_prepare_guard_rejects_dependency_symlink_added_after_confirmation(
        sandbox_env, enabled_prepare, monkeypatch):
    victim = sandbox_env / 'victim'
    victim.mkdir()
    (victim / 'keep.txt').write_text('safe')
    (sandbox_env / 'requirements.txt').write_text('')
    monkeypatch.setattr(environment, 'isolated_command', sandbox.isolated_command)
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {})

    def redirect_after_preflight(*args, **kwargs):
        (sandbox_env / '.agent-env').symlink_to(victim, target_is_directory=True)
        return True

    monkeypatch.setattr(environment, 'ask_permission', redirect_after_preflight)
    result = environment.prepare_project_environment('uv')
    assert '[退出码]' in result and '[退出码] 0' not in result
    assert sorted(path.name for path in victim.iterdir()) == ['keep.txt']


def test_prepare_uv_script_opt_in_still_rechecks_manifest_after_confirmation(
        sandbox_env, enabled_prepare, monkeypatch):
    manifest = sandbox_env / 'requirements.txt'
    manifest.write_text('')
    monkeypatch.setattr(environment, 'isolated_command', sandbox.isolated_command)
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {})

    def change_manifest(*args, **kwargs):
        manifest.write_text('./unexpected-project\n')
        return True

    monkeypatch.setattr(environment, 'ask_permission', change_manifest)
    result = environment.prepare_project_environment('uv', allow_scripts=True)
    assert '[退出码] 0' not in result and 'manifest changed after confirmation' in result
    assert not (sandbox_env / '.agent-env').exists()


def test_prepare_npm_lock_uses_ci_and_safe_default_flags(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    (sandbox_env / 'package-lock.json').write_text('{}')
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('npm')
    command = _command_text(calls)
    assert 'npm ci' in command
    assert '--ignore-scripts' in command
    assert '--no-audit' in command and '--no-fund' in command
    assert '.agent-env/npm-cache' in command
    assert '退出码' in result or 'exit' in result.lower()


def test_prepare_npm_without_lock_uses_install(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    calls = _record_execution(monkeypatch)
    environment.prepare_project_environment('npm')
    assert 'npm install' in _command_text(calls)


def test_prepare_npm_explicit_script_opt_in_removes_ignore_flag(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    calls = _record_execution(monkeypatch)
    environment.prepare_project_environment('npm', allow_scripts=True)
    assert '--ignore-scripts' not in _command_text(calls)


def test_prepare_uv_lock_uses_locked_isolated_environment(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'pyproject.toml').write_text('[project]\nname="example"\nversion="0.1"\n')
    (sandbox_env / 'uv.lock').write_text('version = 1\n')
    calls = _record_execution(monkeypatch)
    environment.prepare_project_environment('uv')
    command = _command_text(calls)
    assert '--locked' in command and '--no-config' in command
    assert '.agent-env' in command
    assert '.venv' not in command.replace('.agent-env', '')
    assert '--no-build' in command and '--no-install-project' in command


@pytest.mark.parametrize('project', [True, False])
def test_prepare_uv_explicit_scripts_allows_build_and_discloses_manifest(
        sandbox_env, enabled_prepare, monkeypatch, project):
    if project:
        (sandbox_env / 'pyproject.toml').write_text('[project]\nname="example"\nversion="0.1"\n')
        (sandbox_env / 'uv.lock').write_text('version = 1\n')
    else:
        (sandbox_env / 'requirements.txt').write_text('example==1.0\n')
    calls = _record_execution(monkeypatch)
    plans = []
    monkeypatch.setattr(environment, 'ask_permission', lambda *args, **kwargs: plans.append((args, kwargs)) or True)
    result = environment.prepare_project_environment('uv', allow_scripts=True)
    assert '[退出码] 0' in result
    command = _command_text(calls)
    assert '--no-build' not in command and '--no-install-project' not in command
    assert '--only-binary' not in command
    assert '--locked' in command if project else '-r requirements.txt' in command
    plan = json.loads(plans[0][0][1])
    assert plan['allow_scripts'] is True
    assert plan['script_policy'] == '允许源码构建和项目安装脚本'
    assert set(plan['manifest_sha256']) == ({'pyproject.toml', 'uv.lock'} if project else {'requirements.txt'})
    assert plans[0][1]['force_confirmation'] is True


def test_prepare_uv_requirements_uses_binary_only_python(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'requirements.txt').write_text('example==1.0\n')
    calls = _record_execution(monkeypatch)
    environment.prepare_project_environment('uv')
    command = _command_text(calls)
    assert 'uv pip install' in command
    assert '.agent-env/python/bin/python' in command
    assert '-r requirements.txt' in command
    assert '--no-config' in command
    assert '--no-build' in command or '--only-binary' in command


def test_prepare_uv_local_source_build_requires_opt_in_in_real_sandbox(
        sandbox_env, enabled_prepare, monkeypatch):
    import shutil
    from pathlib import Path

    uv_path = shutil.which('uv')
    if not uv_path or Path(uv_path).read_bytes()[:4] != b'\x7fELF':
        pytest.skip('需要独立 Linux uv ELF 验证真实本地源码构建')
    source = sandbox_env / 'source'
    source.mkdir()
    (sandbox_env / 'requirements.txt').write_text('./source\n')
    (source / 'pyproject.toml').write_text(
        '[project]\nname="sandbox_build_fixture"\nversion="1.0"\n'
        '[build-system]\nrequires=[]\nbuild-backend="fixture_backend"\nbackend-path=["."]\n')
    (source / 'fixture_backend.py').write_text('''
from pathlib import Path
import zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    Path('build-executed').write_text('source backend executed')
    name = 'sandbox_build_fixture-1.0-py3-none-any.whl'
    info = 'sandbox_build_fixture-1.0.dist-info/'
    with zipfile.ZipFile(Path(wheel_directory) / name, 'w') as wheel:
        wheel.writestr('sandbox_build_fixture.py', 'VALUE = 42\\n')
        wheel.writestr(info + 'METADATA', 'Metadata-Version: 2.1\\nName: sandbox_build_fixture\\nVersion: 1.0\\n')
        wheel.writestr(info + 'WHEEL', 'Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')
        wheel.writestr(info + 'RECORD', '')
    return name
''')
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'uv': uv_path})
    monkeypatch.setattr(environment, 'isolated_command', sandbox.isolated_command)
    denied = environment.prepare_project_environment('uv')
    assert '[退出码] 0' not in denied, denied
    assert not (source / 'build-executed').exists()
    allowed = environment.prepare_project_environment('uv', allow_scripts=True)
    assert '[退出码] 0' in allowed, allowed
    assert (source / 'build-executed').is_file()
    assert (sandbox_env / '.agent-env/python/lib').is_dir()


@pytest.mark.parametrize('policy', ['readonly', 'smart'])
def test_prepare_denied_confirmation_never_executes(sandbox_env, enabled_prepare, monkeypatch, policy):
    (sandbox_env / 'package.json').write_text('{}')
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: False)
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('npm')
    assert '取消' in result or '拒绝' in result or '拦截' in result
    assert calls == []


def test_prepare_confirmation_timeout_never_executes(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    monkeypatch.setattr(sandbox, 'confirmer', None)
    monkeypatch.setattr(sandbox.select, 'select', lambda *args: ([], [], []))
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('npm')
    assert '取消' in result
    assert calls == []


def test_prepare_network_request_requires_allowlist(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'off')
    calls = _record_execution(monkeypatch)
    result = environment.prepare_project_environment('npm', network=True)
    assert '拦截' in result or '网络' in result or '联网' in result
    assert calls == []


def test_prepare_reports_failed_execution_without_claiming_validation(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    calls = _record_execution(monkeypatch, '[退出码] 7\n[stderr]\ninstall failed')
    result = environment.prepare_project_environment('npm')
    assert len(calls) == 1
    assert '7' in result and 'install failed' in result
    assert '测试通过' not in result


def test_prepare_reports_timeout_without_claiming_success(sandbox_env, enabled_prepare, monkeypatch):
    (sandbox_env / 'package.json').write_text('{}')
    calls = _record_execution(monkeypatch, '[超时] 命令超过 120 秒未结束，已终止。')
    result = environment.prepare_project_environment('npm')
    assert len(calls) == 1
    assert '超时' in result
    assert '测试通过' not in result
