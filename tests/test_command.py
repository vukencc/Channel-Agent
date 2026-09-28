"""Real isolated commands, confirmation, host boundaries and process cleanup."""
import json
import shutil


from ai_agent_startup import config
from ai_agent_startup.tools import sandbox
from ai_agent_startup.tools.command import run_command


def test_run_simple_command(sandbox_env):
    assert '[退出码] 0' in run_command('ls -la')


def test_command_runs_in_sandbox_cwd(sandbox_env):
    (sandbox_env / 'marker.txt').write_text('x')
    assert 'marker.txt' in run_command('ls')
    assert '/workspace' in run_command('pwd')


def test_nonzero_exit_code_reported(sandbox_env):
    result = run_command('ls missing-file')
    assert '[退出码] 0' not in result
    assert '[stderr]' in result


def test_shell_metacharacters_are_interpreted_inside_isolation(sandbox_env):
    result = run_command('echo a; echo b')
    assert '[退出码] 0' in result
    assert 'a\nb' in result


def test_python_shell_and_file_commands_allowed(sandbox_env):
    assert '[退出码] 0' in run_command("python3 -c 'from pathlib import Path; Path(\"a.txt\").write_text(\"hello\")'")
    assert (sandbox_env / 'a.txt').read_text() == 'hello'
    result = run_command("sh -c 'mkdir notes; cp a.txt notes/b.txt; mv notes/b.txt notes/c.txt; cat notes/c.txt | wc -c; rm a.txt; rmdir empty 2>/dev/null || true'")
    assert '[退出码] 0' in result
    assert (sandbox_env / 'notes/c.txt').read_text() == 'hello'
    assert not (sandbox_env / 'a.txt').exists()


def test_absolute_program_path_inside_container_allowed(sandbox_env):
    assert '[退出码] 0' in run_command('/bin/ls')


def test_host_files_symlinks_and_environment_hidden(sandbox_env, monkeypatch):
    outside = sandbox_env.parent / 'secret.txt'
    outside.write_text('host-private-sentinel')
    (sandbox_env / 'link').symlink_to(outside)
    monkeypatch.setenv('SANDBOX_TEST_SECRET', 'host-private-sentinel')
    script = ('import os; from pathlib import Path; '
              'assert "SANDBOX_TEST_SECRET" not in os.environ; '
              f'assert not Path({str(outside)!r}).exists(); '
              'assert not Path("link").exists(); '
              'assert not Path("../secret.txt").exists(); print("isolated")')
    import shlex
    result = run_command('python3 -c ' + shlex.quote(script))
    assert '[退出码] 0' in result
    assert 'isolated' in result
    assert outside.read_text() == 'host-private-sentinel'


def test_system_files_readonly(sandbox_env):
    result = run_command("sh -c 'touch /usr/sandbox-write-probe'")
    assert '[退出码] 0' not in result
    assert 'Read-only' in result


def test_missing_isolation_never_runs_host_command(sandbox_env, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: None)
    asked = []
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: asked.append(1) or True)
    assert '[已拦截]' in run_command('touch unsafe.txt')
    assert not (sandbox_env / 'unsafe.txt').exists()
    assert asked == []


def test_command_denied_by_user(sandbox_env, monkeypatch):
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: False)
    assert '[已取消]' in run_command('touch denied.txt')
    assert not (sandbox_env / 'denied.txt').exists()


def test_confirm_timeout_aborts_command(sandbox_env, monkeypatch):
    monkeypatch.setattr(sandbox, 'confirmer', None)
    monkeypatch.setattr(sandbox.select, 'select', lambda *args: ([], [], []))
    assert '[已取消]' in run_command('ls')


def test_command_timeout(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_TIMEOUT', 0.3)
    assert '[超时]' in run_command('sleep 5')


def test_output_truncated(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 20)
    result = run_command('echo ' + 'a' * 200)
    assert '已截断' in result
    assert result.count('a') < 200


def test_invalid_command_and_audit(sandbox_env):
    assert '[stderr]' in run_command("echo '")
    assert '[已拦截]' in run_command("echo \x00")
    run_command('ls')
    records = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    assert {'blocked', 'confirm', 'executed'} <= {row['event'] for row in records}


def test_network_cannot_reach_host_listener(sandbox_env):
    import socket
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = run_command(f"python3 -c 'import socket; s=socket.socket(); s.settimeout(0.5); print(s.connect_ex((\"127.0.0.1\", {port})))'")
        assert '[退出码] 0' in result
        assert result.split('[stdout]\n')[1].strip() != '0'


def test_timeout_terminates_spawned_children(sandbox_env, monkeypatch):
    import time
    monkeypatch.setattr(config, 'COMMAND_TIMEOUT', 0.2)
    result = run_command("sh -c '(sleep 0.7; touch escaped-child) & wait'")
    assert '[超时]' in result
    time.sleep(0.9)
    assert not (sandbox_env / 'escaped-child').exists()


def test_session_cancellation_terminates_running_command(sandbox_env):
    import threading
    import time
    from ai_agent_startup.tools.sandbox import ToolContext, tool_context
    cancelled = threading.Event()
    timer = threading.Timer(0.2, cancelled.set)
    timer.start()
    start = time.monotonic()
    try:
        with tool_context(ToolContext(sandbox_env, config.AUDIT_LOG, lambda *args: True, cancelled)):
            result = run_command('sleep 5')
        assert '[已取消]' in result
        assert time.monotonic() - start < 2
    finally:
        timer.cancel()


def test_heredoc_and_syntax_errors_return_without_waiting_for_terminal(sandbox_env, monkeypatch):
    import os
    import time
    import subprocess
    original = subprocess.Popen
    observed = []
    def capture(*args, **kwargs):
        observed.append(kwargs.get('stdin'))
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', capture)
    start = time.monotonic()
    result = run_command("python3 - <<'PYCODE'\nprint(2 + 3)\nPYCODE")
    assert '[stdout]\n5' in result
    result = run_command("python3 - <<'PYCODE'\ndef seg(name,(x1,y1),(x2,y2)):\n    pass\nPYCODE")
    assert 'SyntaxError' in result
    assert '[超时]' not in result
    assert '[退出码] 0' in run_command('python3 -')
    assert time.monotonic() - start < 2
    assert observed == [subprocess.DEVNULL] * 3


def test_heredoc_preserves_quotes_pipes_and_redirects(sandbox_env):
    result = run_command("cat <<'EOF' > quoted.txt\nit's literal: $HOME `id`\nEOF\ncat quoted.txt | wc -l")
    assert '[退出码] 0' in result
    assert (sandbox_env / 'quoted.txt').read_text() == "it's literal: $HOME `id`\n"


def test_large_stdout_and_stderr_are_drained_with_bounded_memory(sandbox_env, monkeypatch):
    import tracemalloc
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 1000)
    tracemalloc.start()
    try:
        result = run_command("python3 - <<'PYCODE'\nimport os\nfor _ in range(1000):\n    os.write(1, b'a'*10000)\n    os.write(2, b'b'*10000)\nPYCODE")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert '[退出码] 0' in result
    assert '[stdout]' in result and '[stderr]' in result
    assert '已截断' in result
    assert len(result) < 2500
    assert peak < 1024 * 1024
