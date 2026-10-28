"""显式本机命令的真实执行、权限、环境与取消回归。"""
import json
import os
import shlex
import shutil
import signal
import sys
import threading
import time

import pytest

from agent_core import config
from agent_core.tools import command, environment, sandbox


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='本机命令要求 Linux')


@pytest.fixture
def local_context(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_EXECUTION_ENVIRONMENT', 'sandbox', raising=False)
    monkeypatch.setattr(config, 'LOCAL_COMMAND_TIMEOUT', 10, raising=False)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    status = sandbox_env.stat()
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
                                 threading.Event(), permission_policy='full_access',
                                 root_identity=(status.st_dev, status.st_ino))
    context.execution_environment = 'local'
    with sandbox.tool_context(context):
        yield context


def _python(source):
    return '/usr/bin/python3 -I -c ' + shlex.quote(source)


def test_local_uses_real_project_path_without_sandbox_or_workspace_quota(local_context, monkeypatch):
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    (local_context.root / 'existing').write_text('user project')
    monkeypatch.setattr(command, 'isolated_command', lambda *_a, **_k: pytest.fail('local 不能调用 bwrap'))
    result = command.run_command(_python('import os; print(os.getcwd()); open("created", "w").write("ok")'),
                                 runtime_profile='node', resource_profile='development', network=True)
    assert '[退出码] 0' in result, result
    assert str(local_context.root) in result
    assert (local_context.root / 'created').read_text() == 'ok'


def test_local_preserves_linux_path_but_does_not_inherit_api_secrets(local_context, monkeypatch, tmp_path):
    binary = tmp_path / 'linux-bin'
    binary.mkdir()
    java = binary / 'java'
    java.write_text('#!/bin/sh\nprintf local-java-visible\\n\n')
    java.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + ':/usr/bin:/bin:/mnt/c/Windows/System32')
    monkeypatch.setenv('OPENAI_API_KEY', 'private-fixture-value')
    monkeypatch.setenv('CUSTOM_MODEL_TOKEN', 'private-fixture-token')
    result = command.run_command('java && ' + _python(
        'import os; print("secrets", "OPENAI_API_KEY" in os.environ, "CUSTOM_MODEL_TOKEN" in os.environ); '
        'print("windows", "/mnt/c/" in os.environ["PATH"])'))
    assert '[退出码] 0' in result and 'local-java-visible' in result, result
    assert 'secrets False False' in result and 'windows False' in result
    assert 'private-fixture-value' not in local_context.audit_path.read_text()


def test_local_path_adds_configured_linux_runtime_directories(local_context, monkeypatch, tmp_path):
    node = tmp_path / 'node-prefix/bin'
    uv = tmp_path / 'uv-bin'
    node.mkdir(parents=True)
    uv.mkdir()
    for path, name in ((node / 'node', 'node-configured'), (uv / 'uv', 'uv-configured')):
        path.write_text('#!/bin/sh\nprintf ' + name + '\\n\n')
        path.chmod(0o755)
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(node.parent), 'uv': str(uv / 'uv')})
    result = command.run_command('node && uv')
    assert '[退出码] 0' in result and 'node-configured' in result and 'uv-configured' in result


@pytest.mark.parametrize('variable', ['http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
                                      'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'])
def test_local_preserves_system_proxy_configuration(local_context, monkeypatch, variable):
    # 代理属于本机网络配置，不能像模型密钥一样过滤掉。
    value = 'internal.example.invalid' if variable.lower() == 'no_proxy' else 'http://127.0.0.1:3128'
    monkeypatch.setenv(variable, value)
    monkeypatch.setenv('OPENAI_API_KEY', 'private-fixture-key')
    check = (repr(value) + ' in os.environ.get(' + repr(variable) + ', "").split(",")'
             if variable.lower() == 'no_proxy' else 'os.environ.get(' + repr(variable) + ') == ' + repr(value))
    result = command.run_command(_python(
        'import os; print("proxy_preserved", ' + check + '); '
        'print("model_key_inherited", "OPENAI_API_KEY" in os.environ)'))
    assert '[退出码] 0' in result and 'proxy_preserved True' in result, result
    assert 'model_key_inherited False' in result


def test_local_download_uses_configured_proxy(local_context, monkeypatch):
    import http.server

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'proxy-download-ok')

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:' + str(server.server_port))
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setenv('NO_PROXY', '')
    try:
        result = command.run_command(_python(
            'import urllib.request; print(urllib.request.urlopen('
            '"http://proxy-regression.invalid/toolchain", timeout=2).read().decode())'))
        assert '[退出码] 0' in result and 'proxy-download-ok' in result, result
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_local_can_access_localhost_without_sandbox_network_flag(local_context):
    import http.server
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), http.server.SimpleHTTPRequestHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = command.run_command(_python(
            'import urllib.request; print(urllib.request.urlopen('
            + repr('http://127.0.0.1:' + str(server.server_port)) + ', timeout=2).status)'), network=False)
        assert '[退出码] 0' in result and '200' in result, result
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize('policy', ['readonly', 'trusted', 'smart'])
def test_local_selected_policy_denial_never_executes(local_context, policy, monkeypatch):
    local_context.permission_policy = policy
    local_context.confirm = lambda *_: False
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [
        {'tool': 'run_command', 'path_prefix': None, 'command_prefix': ['touch']}])
    result = command.run_command('touch forbidden')
    assert '取消' in result
    assert not (local_context.root / 'forbidden').exists()


def test_local_full_access_policy_remains_automatic(local_context):
    local_context.confirm = lambda *_: pytest.fail('full_access 不应要求额外确认')
    assert '[退出码] 0' in command.run_command('printf approved')
    rows = [json.loads(line) for line in local_context.audit_path.read_text().splitlines()]
    assert any(row.get('decision_source') == 'full_access' and row.get('allowed') for row in rows)


def test_local_rejects_non_linux_platform_before_execution(local_context, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    result = command.run_command('touch forbidden')
    assert '拦截' in result and 'Linux' in result
    assert not (local_context.root / 'forbidden').exists()


def test_local_root_identity_is_rechecked_after_confirmation(local_context):
    original = local_context.root
    local_context.permission_policy = 'smart'

    def replace_root(*_):
        original.rename(original.with_name('old-project'))
        original.mkdir()
        return True

    local_context.confirm = replace_root
    result = command.run_command('touch forbidden')
    assert '拦截' in result and '替换' in result, result
    assert not (original / 'forbidden').exists()


def test_local_does_not_apply_sandbox_cpu_file_or_memory_rlimits(local_context, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_CPU_SECONDS', 1)
    monkeypatch.setattr(config, 'COMMAND_FILE_MB', 1)
    monkeypatch.setattr(config, 'COMMAND_MEMORY_MB', 1)
    result = command.run_command(_python(
        'import resource; assert resource.getrlimit(resource.RLIMIT_CPU)[0] != 1; '
        'open("large", "wb").write(b"x" * (2 * 1024 * 1024)); print("unrestricted")'))
    assert '[退出码] 0' in result and 'unrestricted' in result, result


def test_sandbox_failure_never_falls_back_to_local(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_EXECUTION_ENVIRONMENT', 'sandbox', raising=False)
    monkeypatch.setattr(command, 'isolated_command', lambda *_a, **_k: (_ for _ in ()).throw(sandbox.SandboxError('no bwrap')))
    result = command.run_command('touch forbidden')
    assert '拦截' in result and not (sandbox_env / 'forbidden').exists()


def test_local_inspection_sees_real_runtimes_without_sandbox(local_context, monkeypatch):
    monkeypatch.setattr(environment, 'isolated_command', lambda *_a, **_k: pytest.fail('local probe 不能调用 bwrap'))
    result = json.loads(environment.inspect_environment())
    assert result['execution_environment'] == 'local', result
    assert result['runtimes']['python']['status'] == 'available'
    assert result['memory_limit_mb'] is None


def test_local_prepare_is_explicitly_enabled_and_uses_real_path(local_context, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_ENVIRONMENT_PREPARE', False)
    (local_context.root / 'package.json').write_text('{"name":"local-test","version":"1.0.0"}')
    monkeypatch.setattr(environment, 'isolated_command', lambda *_a, **_k: pytest.fail('local prepare 不能调用 bwrap'))
    calls = []
    monkeypatch.setattr(environment, '_execute', lambda *args, **kwargs: calls.append((args, kwargs)) or '[退出码] 0\n')
    result = environment.prepare_project_environment('npm')
    assert '[退出码] 0' in result, result
    assert str(local_context.root) in calls[0][0][0]
    assert '/workspace' not in calls[0][0][0]
    assert '--ignore-scripts' in calls[0][0][0]
    assert calls[0][1]['check_quota'] is False
    assert '--offline' not in calls[0][0][0]


def test_local_prepare_npm_executes_real_install_and_requires_script_opt_in(local_context):
    if not shutil.which('npm') or not shutil.which('node'):
        pytest.skip('真实本机 npm 准备需要 Linux Node/npm')
    manifest = {'name': 'local-prepare-fixture', 'version': '1.0.0', 'scripts': {
        'postinstall': 'node -e "require(\'fs\').writeFileSync(\'install-script-ran\', \'yes\')"'}}
    (local_context.root / 'package.json').write_text(json.dumps(manifest))
    result = environment.prepare_project_environment('npm')
    assert '[退出码] 0' in result, result
    assert not (local_context.root / 'install-script-ran').exists()
    result = environment.prepare_project_environment('npm', allow_scripts=True)
    assert '[退出码] 0' in result, result
    assert (local_context.root / 'install-script-ran').read_text() == 'yes'


def test_local_prepare_uv_executes_real_install_without_replacing_user_venv(local_context):
    if not shutil.which('uv'):
        pytest.skip('真实本机 Python 准备需要 Linux uv')
    (local_context.root / 'requirements.txt').write_text('')
    (local_context.root / '.venv').mkdir()
    (local_context.root / '.venv/keep').write_text('user-owned')
    result = environment.prepare_project_environment('uv')
    assert '[退出码] 0' in result, result
    assert (local_context.root / '.venv/keep').read_text() == 'user-owned'
    assert (local_context.root / '.agent-env/python/.agent-managed').is_file()


def test_local_prepare_rechecks_manifest_after_confirmation(local_context):
    manifest = local_context.root / 'requirements.txt'
    manifest.write_text('')
    local_context.permission_policy = 'smart'

    def change_manifest(*_):
        manifest.write_text('./unexpected-project\n')
        return True

    local_context.confirm = change_manifest
    result = environment.prepare_project_environment('uv', allow_scripts=True)
    assert '[退出码] 0' not in result and 'manifest changed after confirmation' in result
    assert not (local_context.root / '.agent-env').exists()


def test_local_prepare_rechecks_root_identity_after_confirmation(local_context):
    root = local_context.root
    (root / 'requirements.txt').write_text('')
    local_context.permission_policy = 'smart'

    def replace_root(*_):
        root.rename(root.with_name('old-project'))
        root.mkdir()
        (root / 'requirements.txt').write_text('')
        return True

    local_context.confirm = replace_root
    result = environment.prepare_project_environment('uv')
    assert '拦截' in result and '替换' in result
    assert not (root / '.agent-env').exists()


def test_local_timeout_is_bounded_and_kills_detached_child(local_context, monkeypatch):
    monkeypatch.setattr(config, 'LOCAL_COMMAND_TIMEOUT', .4)
    result = command.run_command(_python(
        'import subprocess,time; child=subprocess.Popen(["/bin/sh", "-c", '
        '"sleep 1; touch detached-sentinel"], start_new_session=True); '
        'open("child.pid", "w").write(str(child.pid)); time.sleep(5)'))
    assert '[超时]' in result, result
    time.sleep(1.1)
    assert not (local_context.root / 'detached-sentinel').exists()


@pytest.fixture
def captured_local_processes(monkeypatch):
    """保留真实 Popen 句柄，失败回归自身也必须回收临时宿主进程。"""
    original = command.subprocess.Popen
    processes = []

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(command.subprocess, 'Popen', capture)
    try:
        yield processes
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for pipe in (process.stdout, process.stderr):
                pipe.close()


@pytest.mark.parametrize('failure', [PermissionError, OSError])
def test_local_tracking_initialization_failure_reaps_host_process(
        local_context, monkeypatch, captured_local_processes, failure):
    from agent_core.tools.execution_environment import LocalProcessTree

    def fail_snapshot():
        raise failure('proc enumeration denied')

    monkeypatch.setattr(LocalProcessTree, '_snapshot', staticmethod(fail_snapshot))
    result = command.run_command(_python(
        'import time; time.sleep(.25); open("initialization-sentinel", "w").write("leaked")'))
    time.sleep(.4)
    assert not (local_context.root / 'initialization-sentinel').exists(), result
    assert result.startswith('[失败]'), result
    assert len(captured_local_processes) == 1
    process = captured_local_processes[0]
    assert process.returncode is not None
    assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize('stop', ['cancelled', 'timeout'])
def test_local_tracking_cleanup_failure_preserves_stop_result_and_reaps_host_process(
        local_context, monkeypatch, captured_local_processes, stop):
    from agent_core.tools.execution_environment import LocalProcessTree

    original_terminate = LocalProcessTree.terminate

    def fail_snapshot():
        raise PermissionError('private-proc-tracking-detail')

    def fail_during_termination(tree):
        monkeypatch.setattr(LocalProcessTree, '_snapshot', staticmethod(fail_snapshot))
        original_terminate(tree)

    monkeypatch.setattr(LocalProcessTree, 'terminate', fail_during_termination)
    monkeypatch.setattr(config, 'LOCAL_COMMAND_TIMEOUT', .15 if stop == 'timeout' else 5)
    timer = threading.Timer(.15, local_context.cancelled.set) if stop == 'cancelled' else None
    if timer is not None:
        timer.start()
    try:
        result = command.run_command(_python(
            'import time; time.sleep(.5); open("cleanup-sentinel", "w").write("leaked")'))
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
    time.sleep(.6)
    assert not (local_context.root / 'cleanup-sentinel').exists(), result
    assert result.startswith('[已取消]' if stop == 'cancelled' else '[超时]'), result
    assert len(captured_local_processes) == 1
    process = captured_local_processes[0]
    assert process.returncode is not None
    assert process.stdout.closed and process.stderr.closed
    assert 'private-proc-tracking-detail' not in local_context.audit_path.read_text()


@pytest.mark.parametrize('failure', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('phase', ['initialization', 'cleanup'])
def test_local_tracking_interrupt_propagates_after_process_cleanup(
        local_context, monkeypatch, captured_local_processes, failure, phase):
    from agent_core.tools.execution_environment import LocalProcessTree

    def interrupt(*_):
        raise failure()

    if phase == 'initialization':
        monkeypatch.setattr(LocalProcessTree, '_snapshot', staticmethod(interrupt))
    else:
        monkeypatch.setattr(LocalProcessTree, 'terminate', interrupt)
        monkeypatch.setattr(config, 'LOCAL_COMMAND_TIMEOUT', .1)
    with pytest.raises(failure):
        command.run_command(_python(
            'import time; time.sleep(.25); open("interrupt-sentinel", "w").write("leaked")'))
    time.sleep(.4)
    assert not (local_context.root / 'interrupt-sentinel').exists()
    assert len(captured_local_processes) == 1
    process = captured_local_processes[0]
    assert process.returncode is not None
    assert process.stdout.closed and process.stderr.closed


@pytest.fixture
def local_jobs(local_context, tmp_path, monkeypatch):
    from agent_core.core.command_jobs import CommandJobs
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    jobs = CommandJobs(store)
    local_context.job_manager = jobs
    try:
        yield jobs
    finally:
        jobs.close()
        store.close()


def _wait_job(jobs, context, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = jobs.status(context.root.name, identifier)
        if record['status'] not in {'queued', 'running'}:
            return record
        time.sleep(.02)
    pytest.fail('本机后台命令未结束')


def test_local_jobs_enabled_by_explicit_mode_and_persist_environment(local_context, local_jobs, monkeypatch):
    from agent_core.core import command_jobs
    monkeypatch.setattr(command_jobs, 'isolated_command', lambda *_a, **_k: pytest.fail('local job 不能调用 bwrap'))
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    (local_context.root / 'existing').write_text('user project')
    identifier = local_jobs.start('printf local-job', network=True, runtime_profile='node')
    record = _wait_job(local_jobs, local_context, identifier)
    assert record['status'] == 'completed', record
    assert record['execution_environment'] == 'local'
    assert record['timeout'] == config.LOCAL_COMMAND_TIMEOUT
    assert local_jobs.logs(local_context.root.name, identifier) == 'local-job'


def test_local_job_mode_is_frozen_while_queued(local_context, local_jobs, monkeypatch):
    release = threading.Event()
    blockers = [local_jobs.executor.submit(release.wait, 5)
                for _ in range(config.COMMAND_JOB_CONCURRENCY)]
    try:
        identifier = local_jobs.start('printf frozen', timeout=2)
        local_context.execution_environment = 'sandbox'
    finally:
        release.set()
        for future in blockers:
            future.result(5)
    assert _wait_job(local_jobs, local_context, identifier)['status'] == 'completed'
    assert local_jobs.records[identifier]['execution_environment'] == 'local'


def test_local_job_cancellation_kills_sets_id_child(local_context, local_jobs):
    identifier = local_jobs.start(_python(
        'import subprocess,time; subprocess.Popen(["/bin/sh", "-c", '
        '"sleep 1; touch job-sentinel"], start_new_session=True); '
        'open("job-started", "w").write("yes"); time.sleep(5)'), timeout=5)
    deadline = time.monotonic() + 3
    while not (local_context.root / 'job-started').exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    time.sleep(.15)
    local_jobs.cancel(local_context.root.name, identifier)
    assert _wait_job(local_jobs, local_context, identifier)['status'] == 'cancelled'
    time.sleep(1.1)
    assert not (local_context.root / 'job-sentinel').exists()


def test_local_job_denied_selected_policy_does_not_queue(local_context, local_jobs):
    local_context.permission_policy = 'readonly'
    with pytest.raises(PermissionError):
        local_jobs.start('touch forbidden')
    assert local_jobs.records == {} and not (local_context.root / 'forbidden').exists()


def test_local_server_job_does_not_lock_foreground_workspace_access(local_context, local_jobs):
    identifier = local_jobs.start(_python(
        'import http.server; server=http.server.HTTPServer(("127.0.0.1", 0), '
        'http.server.SimpleHTTPRequestHandler); open("server.port", "w").write(str(server.server_port)); '
        'server.serve_forever()'), timeout=3)
    try:
        deadline = time.monotonic() + 2
        while not (local_context.root / 'server.port').exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        port = (local_context.root / 'server.port').read_text()
        started = time.monotonic()
        # 与 SessionManager._tool 相同的前台工作区锁；服务存活时必须可进入。
        with local_jobs.guard(local_context.root, threading.Event()):
            result = command.run_command(_python(
                'import urllib.request; print(urllib.request.urlopen('
                + repr('http://127.0.0.1:' + port) + ', timeout=1).status)'))
        assert '[退出码] 0' in result and '200' in result, result
        assert time.monotonic() - started < 1
        assert local_jobs.status(local_context.root.name, identifier)['status'] == 'running'
    finally:
        local_jobs.cancel(local_context.root.name, identifier)
        _wait_job(local_jobs, local_context, identifier)
