import shutil

from ai_agent_startup import config
from ai_agent_startup.tools.command import run_command
from ai_agent_startup.tools.sandbox import isolated_command, SandboxError
import pytest


def test_overfull_workspace_refuses_before_execution(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', .001, raising=False)
    (sandbox_env / 'existing').write_bytes(b'x' * 2000)
    result = run_command('touch forbidden')
    assert '配额' in result
    assert not (sandbox_env / 'forbidden').exists()


def test_missing_limit_launcher_fails_closed(sandbox_env, monkeypatch):
    original = shutil.which
    monkeypatch.setattr(shutil, 'which', lambda name, **kw: None if name == 'prlimit' else original(name, **kw))
    with pytest.raises(SandboxError, match='prlimit'):
        isolated_command(['/bin/true'])


def test_file_size_limit_stops_large_write(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_FILE_MB', 1, raising=False)
    result = run_command('python3 -c "open(\'large\', \'wb\').write(b\'x\' * 3000000)"')
    assert '[退出码] 0' not in result
    assert (sandbox_env / 'large').stat().st_size <= 1024 * 1024


def test_process_limit_refuses_bounded_fork_probe(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_PROCESSES', 12)
    result = run_command("python3 - <<'PYCODE'\nimport os, time\nchildren=[]\nblocked=False\nfor _ in range(32):\n    try:\n        pid=os.fork()\n    except OSError:\n        blocked=True\n        break\n    if pid == 0:\n        time.sleep(.2)\n        os._exit(0)\n    children.append(pid)\nfor pid in children:\n    os.waitpid(pid, 0)\nprint('blocked', blocked, 'spawned', len(children))\nPYCODE")
    assert '[退出码] 0' in result
    assert 'blocked True' in result


def test_rlimit_capability_failure_refuses_execution(sandbox_env, monkeypatch):
    import resource
    def denied(*args):
        raise OSError('unsupported')
    monkeypatch.setattr(resource, 'getrlimit', denied)
    result = run_command('touch never')
    assert '探测失败' in result
    assert not (sandbox_env / 'never').exists()
