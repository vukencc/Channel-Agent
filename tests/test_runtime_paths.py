"""Explicit Linux runtime mounts keep host data and command quotas isolated."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from ai_agent_startup import config
from ai_agent_startup.tools import sandbox
from ai_agent_startup.tools.command import run_command
from ai_agent_startup.tools.runtime_paths import RuntimeBindingError, build_runtime_bindings


@pytest.fixture(autouse=True)
def explicit_runtime_defaults(monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {}, raising=False)
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 0, raising=False)


@pytest.fixture
def runtime_prefix(tmp_path):
    prefix = tmp_path / 'runtime' / 'node'
    (prefix / 'bin').mkdir(parents=True)
    shutil.copyfile('/usr/bin/true', prefix / 'bin/node')
    (prefix / 'bin/node').chmod(0o755)
    npm = prefix / 'lib/node_modules/npm'
    (npm / 'bin').mkdir(parents=True)
    (npm / 'bin/npm-cli.js').write_text('console.log("npm")')
    (npm / 'bin/npx-cli.js').write_text('console.log("npx")')
    (prefix / 'lib/node_modules/private-global').mkdir()
    (prefix / 'lib/node_modules/private-global/secret').write_text('private-global')
    return prefix


def test_empty_runtime_configuration_preserves_default_path(sandbox_env):
    bindings = build_runtime_bindings({}, workspace=sandbox_env)
    assert bindings.mount_args == [] and bindings.environment == {}
    command = sandbox.isolated_command(['/bin/true'])
    offset = command.index('PATH')
    assert command[offset + 1] == '/usr/bin:/bin'
    assert '/run/agent-runtime' not in command


def test_node_mounts_only_binary_npm_and_trusted_launchers(runtime_prefix, sandbox_env):
    bindings = build_runtime_bindings({'node': str(runtime_prefix)}, workspace=sandbox_env)
    sources = [bindings.mount_args[index + 1] for index, arg in enumerate(bindings.mount_args)
               if arg == '--ro-bind']
    assert str(runtime_prefix / 'bin/node') in sources
    assert str(runtime_prefix / 'lib/node_modules/npm') in sources
    assert str(runtime_prefix) not in sources
    assert all('private-global' not in source for source in sources)
    assert bindings.environment['PATH'] == '/run/agent-runtime/bin:/usr/local/bin:/usr/bin:/bin'
    assert all(source != os.environ.get('PATH') for source in sources)


@pytest.mark.parametrize('mapping', [None, [], {'java': '/usr/lib/jvm'}, {'maven': '/opt/maven'},
                                      {'node': 'D:\\js\\run'}, {'node': '/'}])
def test_invalid_or_unsupported_runtime_binding_fails_closed(mapping, sandbox_env):
    with pytest.raises(RuntimeBindingError):
        build_runtime_bindings(mapping, workspace=sandbox_env)


def test_windows_pe_binary_is_rejected(runtime_prefix, sandbox_env):
    (runtime_prefix / 'bin/node').write_bytes(b'MZ' + bytes(128))
    with pytest.raises(RuntimeBindingError, match='ELF'):
        build_runtime_bindings({'node': str(runtime_prefix)}, workspace=sandbox_env)


def test_wrong_architecture_binary_is_rejected(runtime_prefix, sandbox_env):
    node = runtime_prefix / 'bin/node'
    data = bytearray(node.read_bytes())
    data[18:20] = bytes(2)
    node.write_bytes(data)
    with pytest.raises(RuntimeBindingError, match='架构'):
        build_runtime_bindings({'node': str(runtime_prefix)}, workspace=sandbox_env)


def test_symlink_runtime_prefix_is_rejected(runtime_prefix, sandbox_env, tmp_path):
    link = tmp_path / 'linked-node'
    link.symlink_to(runtime_prefix, target_is_directory=True)
    with pytest.raises(RuntimeBindingError, match='符号链接'):
        build_runtime_bindings({'node': str(link)}, workspace=sandbox_env)


def test_external_npm_symlink_is_rejected(runtime_prefix, sandbox_env, tmp_path):
    private = tmp_path / 'private'
    private.write_text('host-secret')
    (runtime_prefix / 'lib/node_modules/npm/escape').symlink_to(private)
    with pytest.raises(RuntimeBindingError, match='符号链接'):
        build_runtime_bindings({'node': str(runtime_prefix)}, workspace=sandbox_env)


def test_unreadable_npm_subdirectory_fails_closed(runtime_prefix, sandbox_env):
    if os.geteuid() == 0:
        pytest.skip('Read permissions cannot be tested as root')
    unreadable = runtime_prefix / 'lib/node_modules/npm/unreadable'
    unreadable.mkdir()
    unreadable.chmod(0)
    try:
        with pytest.raises(RuntimeBindingError):
            build_runtime_bindings({'node': str(runtime_prefix)}, workspace=sandbox_env)
    finally:
        unreadable.chmod(0o700)


def test_workspace_state_and_credential_overlap_is_rejected(runtime_prefix, sandbox_env):
    for protected in (runtime_prefix, runtime_prefix.parent, runtime_prefix / 'bin/node'):
        with pytest.raises(RuntimeBindingError):
            build_runtime_bindings({'node': str(runtime_prefix)}, workspace=sandbox_env,
                                   protected_paths=(protected,))
    with pytest.raises(RuntimeBindingError):
        build_runtime_bindings({'node': str(runtime_prefix)}, workspace=runtime_prefix)


def test_removed_runtime_fails_closed_before_confirmation(runtime_prefix, sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(runtime_prefix)})
    (runtime_prefix / 'bin/node').unlink()
    asked = []
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: asked.append(True) or True)
    result = run_command('touch unsafe-fallback')
    assert '[已拦截]' in result
    assert asked == []
    assert not (sandbox_env / 'unsafe-fallback').exists()


def test_explicit_runtime_path_and_source_globals_are_isolated(runtime_prefix, sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(runtime_prefix)})
    monkeypatch.setenv('RUNTIME_PRIVATE_SECRET', 'never-inherit')
    script = ('import os; from pathlib import Path; '
              'assert "RUNTIME_PRIVATE_SECRET" not in os.environ; '
              f'assert not Path({str(runtime_prefix)!r}).exists(); '
              'assert not Path("/run/agent-runtime/lib/node_modules/private-global").exists(); '
              'assert "/usr/local/bin" in os.environ["PATH"].split(":"); '
              'print("runtime-isolated")')
    result = run_command('command -v node; python3 -c ' + shlex.quote(script))
    assert '[退出码] 0' in result and 'runtime-isolated' in result
    assert '/run/agent-runtime/bin/node' in result
    result = run_command('touch /run/agent-runtime/bin/node')
    assert '[退出码] 0' not in result and 'Read-only' in result
    assert (runtime_prefix / 'lib/node_modules/private-global/secret').read_text() == 'private-global'


@pytest.mark.parametrize('memory', [0, -1, 8193, True, '2048'])
def test_node_profile_requires_bounded_explicit_memory(runtime_prefix, sandbox_env, monkeypatch, memory):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(runtime_prefix)})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', memory)
    with pytest.raises(sandbox.SandboxError):
        sandbox.isolated_command(['/bin/true'], runtime_profile='node')


def test_node_profile_requires_node_binding(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048)
    with pytest.raises(sandbox.SandboxError):
        sandbox.isolated_command(['/bin/true'], runtime_profile='node')


def test_runtime_profile_preserves_other_quotas(runtime_prefix, sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(runtime_prefix)})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048)
    normal = sandbox.isolated_command(['/bin/true'])
    node = sandbox.isolated_command(['/bin/true'], runtime_profile='node')
    assert f'--as={config.COMMAND_MEMORY_MB * 1024 * 1024}' in normal
    assert '--as=2147483648' in node
    for flag in ('--cpu=', '--fsize=', '--nproc='):
        assert [arg for arg in normal if arg.startswith(flag)] == [arg for arg in node if arg.startswith(flag)]
    with pytest.raises(sandbox.SandboxError):
        sandbox.isolated_command(['/bin/true'], runtime_profile='host')


def _real_node_prefix():
    candidates = sorted((Path.home() / '.nvm/versions/node').glob('*/bin/node'), reverse=True)
    if not candidates:
        pytest.skip('No local Linux Node distribution available for an isolated execution test')
    return candidates[0].parent.parent


def test_real_node_code_runs_only_with_explicit_memory_profile(sandbox_env, monkeypatch, tmp_path):
    prefix = _real_node_prefix()
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(prefix)})
    monkeypatch.setattr(config, 'COMMAND_MEMORY_MB', 512)
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048)
    monkeypatch.setenv('NODE_RUNTIME_PRIVATE_SECRET', 'must-stay-hidden')
    private = tmp_path / 'host-private.txt'
    private.write_text('must-stay-hidden')
    import json
    script = ('const fs = require("node:fs"); const assert = require("node:assert/strict"); '
              'assert.equal(2 + 3, 5); assert.equal(process.cwd(), "/workspace"); '
              'assert.equal(process.env.NODE_RUNTIME_PRIVATE_SECRET, undefined); '
              f'assert.equal(fs.existsSync({json.dumps(str(private))}), false); '
              f'assert.equal(fs.existsSync({json.dumps(str(prefix))}), false); '
              'console.log("node-code-ran")')
    command = 'node -e ' + shlex.quote(script)
    default = subprocess.run(sandbox.isolated_command(sandbox.check_command(command)),
                             capture_output=True, text=True, timeout=10)
    assert default.returncode != 0
    assert 'out of memory' in default.stderr
    actual = subprocess.run(sandbox.isolated_command(sandbox.check_command(command), runtime_profile='node'),
                            capture_output=True, text=True, timeout=10)
    assert actual.returncode == 0, actual.stderr
    assert actual.stdout.strip() == 'node-code-ran'
    versions = subprocess.run(sandbox.isolated_command(sandbox.check_command('node --version; npm --version; npx --version'),
                                                       runtime_profile='node'),
                              capture_output=True, text=True, timeout=10)
    assert versions.returncode == 0, versions.stderr
    assert len(versions.stdout.strip().splitlines()) == 3


def test_real_uv_runs_inside_unchanged_system_quota(sandbox_env, monkeypatch):
    uv = Path.home() / '.local/bin/uv'
    if not uv.is_file() or uv.is_symlink():
        pytest.skip('No standalone local Linux uv executable')
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'uv': str(uv)})
    result = run_command('command -v uv; uv --version; uv cache dir')
    assert '[退出码] 0' in result, result
    assert '/run/agent-runtime/bin/uv' in result
    assert '/home/' not in result
