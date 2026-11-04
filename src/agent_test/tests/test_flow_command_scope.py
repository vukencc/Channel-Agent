"""Flow 本机命令不得写入项目之外。"""
import shlex
import threading
import hashlib
import http.server
import time
import tempfile
import os
import subprocess
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.tools import command, sandbox


def test_flow_empty_workspace_guide_preserves_the_selected_project(tmp_path, monkeypatch):
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    from agent_core.core.session_service import open_session
    for name in ('RAG_ASSESS', 'MEMORY_AUTO_EXTRACT', 'ENABLE_AGENT_TASKS'):
        monkeypatch.setattr(config, name, False)
    directory = tmp_path / 'empty'
    directory.mkdir()
    received = []

    async def model(history, **kwargs):
        received.extend(history)
        return {'role': 'assistant', 'content': '受控空目录验证'}

    async def scenario():
        store = SessionStore(tmp_path / 'state', tmp_path / 'private')
        manager = SessionManager(store, model=model)
        try:
            session = open_session(manager, workspace_path=str(directory), execution_environment='local')
            session.record['flow_id'] = 'a' * 32
            manager.submit(session, '开始项目')
            await session.task
            text = '\n'.join(str(row.get('content', '')) for row in received if row.get('role') == 'system')
            assert str(directory) in text
            assert '空目录就是用户选择的新项目' in text
            assert '只有当前项目工作区可以持久写入' in text
            assert not list(directory.iterdir())
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(scenario())


def test_flow_local_command_cannot_write_outside_workspace(sandbox_env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    external = tmp_path / "protected"
    external.write_text("unchanged")
    info = sandbox_env.stat()
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
        threading.Event(), permission_policy="full_access", execution_environment="local",
        root_identity=(info.st_dev, info.st_ino))
    context.flow_workspace_scope = True
    with sandbox.tool_context(context):
        allowed = command.run_command('pwd; printf ok > inside')
        result = command.run_command("printf changed > " + shlex.quote(str(external)))
    assert '[退出码] 0' in allowed, allowed
    assert str(sandbox_env) in allowed
    assert (sandbox_env / 'inside').read_text() == 'ok'
    assert external.read_text() == "unchanged", result


def test_flow_blocks_multiple_external_write_paths(sandbox_env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    outside = tmp_path / 'outside'
    outside.mkdir()
    secret = outside / 'protected'
    secret.write_text('unchanged')
    digest = hashlib.sha256(secret.read_bytes()).digest()
    (sandbox_env / 'link').symlink_to(secret)
    info = sandbox_env.stat()
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
        threading.Event(), permission_policy='full_access', execution_environment='local',
        root_identity=(info.st_dev, info.st_ino), flow_workspace_scope=True)
    commands = [
        'cd ' + shlex.quote(str(outside)) + ' && printf changed > protected',
        'printf changed > link',
        '/usr/bin/python3 -c ' + shlex.quote('open(' + repr(str(secret)) + ', "w").write("changed")'),
        '/usr/bin/perl -e ' + shlex.quote('open(F, ">", $ARGV[0]) or exit 1; print F "changed"') + ' ' + shlex.quote(str(secret)),
    ]
    with sandbox.tool_context(context):
        for value in commands:
            assert '[退出码] 0' not in command.run_command(value)
    assert hashlib.sha256(secret.read_bytes()).digest() == digest


def test_flow_fails_closed_without_bubblewrap(sandbox_env, monkeypatch):
    from agent_core.tools import execution_environment
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    monkeypatch.setattr(execution_environment.shutil, 'which', lambda *_a, **_k: None)
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
        threading.Event(), permission_policy='full_access', execution_environment='local',
        flow_workspace_scope=True)
    with sandbox.tool_context(context):
        result = command.run_command('touch must-not-run')
    assert '[已拦截]' in result
    assert not (sandbox_env / 'must-not-run').exists()


def test_flow_keeps_localhost_network(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'local-proof')

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 3
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
        threading.Event(), permission_policy='full_access', execution_environment='local',
        flow_workspace_scope=True)
    try:
        with sandbox.tool_context(context):
            result = command.run_command('/usr/bin/python3 -c ' + shlex.quote(
                'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:'
                + str(server.server_port) + '", timeout=2).read().decode())'))
        assert '[退出码] 0' in result and 'local-proof' in result, result
    finally:
        server.server_close()
        thread.join(timeout=3)


def test_flow_background_job_cannot_write_outside(sandbox_env, tmp_path, monkeypatch):
    from agent_core.core.command_jobs import CommandJobs
    from agent_core.core.storage import SessionStore
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    external = tempfile.TemporaryDirectory(prefix='flow-scope-', dir='/var/tmp')
    protected = Path(external.name) / 'protected'
    protected.write_text('unchanged')
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    jobs = CommandJobs(store)
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
        threading.Event(), permission_policy='full_access', execution_environment='local',
        flow_workspace_scope=True, job_manager=jobs)
    try:
        with sandbox.tool_context(context):
            identifier = jobs.start('printf changed > ' + shlex.quote(str(protected)), timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = jobs.status(sandbox_env.name, identifier)
            if record['status'] not in {'queued', 'running'}:
                break
            time.sleep(.02)
        assert record['status'] == 'failed', record
        assert protected.read_text() == 'unchanged'
    finally:
        jobs.close()
        store.close()
        external.cleanup()


def test_flow_keeps_configured_node_and_uv(sandbox_env, monkeypatch):
    versions = Path.home() / '.nvm/versions/node'
    nodes = sorted(versions.glob('*/bin/node')) if versions.is_dir() else []
    uv = Path.home() / '.local/bin/uv'
    if not nodes or not uv.is_file():
        pytest.skip('宿主未提供 NVM Node 和用户 uv，无法验证真实运行时')
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(nodes[-1].parent.parent), 'uv': str(uv)})
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
        threading.Event(), permission_policy='full_access', execution_environment='local',
        flow_workspace_scope=True)
    with sandbox.tool_context(context):
        result = command.run_command('node --version && uv --version')
    assert '[退出码] 0' in result, result


def test_legacy_child_scope_follows_parent_and_missing_parent_fails_closed():
    from agent_core.core.sessions import SessionManager
    parent = SimpleNamespace(id='parent', record={'flow_id': 'flow'})
    child = SimpleNamespace(id='child', record={'delegated_from': 'parent'})
    manager = SimpleNamespace(sessions={'parent': parent, 'child': child})
    assert SessionManager.flow_workspace_scope(manager, child)
    manager.sessions.pop('parent')
    assert SessionManager.flow_workspace_scope(manager, child)
    assert not SessionManager.flow_workspace_scope(manager, SimpleNamespace(id='classic', record={}))


def test_flow_masks_explicit_protected_directory(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    with tempfile.TemporaryDirectory(prefix='flow-private-', dir='/var/tmp') as directory:
        private = Path(directory)
        (private / 'credential').write_text('fixture-secret')
        context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
            threading.Event(), permission_policy='full_access', execution_environment='local',
            flow_workspace_scope=True, protected_paths=(private,))
        with sandbox.tool_context(context):
            result = command.run_command('cat ' + shlex.quote(str(private / 'credential')))
        assert '[退出码] 0' not in result
        assert 'fixture-secret' not in result


def test_flow_rejects_existing_external_hardlink_but_allows_internal(sandbox_env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')
    outside = tmp_path / 'outside-hardlink'
    outside.write_text('unchanged')
    os.link(outside, sandbox_env / 'external-link')
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
        threading.Event(), permission_policy='full_access', execution_environment='local',
        flow_workspace_scope=True)
    with sandbox.tool_context(context):
        blocked = command.run_command('printf changed > external-link')
        assert '[已拦截]' in blocked and '硬链接' in blocked
        assert outside.read_text() == 'unchanged'
        (sandbox_env / 'external-link').unlink()
        (sandbox_env / 'first').write_text('inside')
        os.link(sandbox_env / 'first', sandbox_env / 'second')
        result = command.run_command('printf allowed > second')
        assert '[退出码] 0' in result, result
        assert (sandbox_env / 'first').read_text() == 'allowed'


def test_flow_rejects_nested_mount_entry(sandbox_env, monkeypatch):
    from agent_core.tools import execution_environment
    mount = sandbox_env / 'mount'
    mount.mkdir()
    original = Path.read_text
    def fake_read_text(path, *args, **kwargs):
        if path == Path('/proc/self/mountinfo'):
            return '1 0 0:1 / ' + str(mount) + ' rw - tmpfs tmpfs rw\n'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', fake_read_text)
    with pytest.raises(sandbox.SandboxError, match='嵌套挂载'):
        execution_environment._check_flow_workspace(sandbox_env)


def test_flow_rejects_real_nested_mount(sandbox_env):
    mount = sandbox_env / 'nested'
    mount.mkdir()
    script = '''
import subprocess, sys
from pathlib import Path
from agent_core.tools.execution_environment import _check_flow_workspace
from agent_core.tools.sandbox import SandboxError
mounted = subprocess.run(['/usr/bin/mount', '-t', 'tmpfs', 'tmpfs', sys.argv[2]], capture_output=True)
if mounted.returncode:
    sys.exit(77)
try:
    _check_flow_workspace(Path(sys.argv[1]))
except SandboxError as exc:
    assert '嵌套挂载' in str(exc)
else:
    raise AssertionError('嵌套挂载未被拒绝')
'''
    result = subprocess.run(['/usr/bin/unshare', '-Urnm', sys.executable, '-c', script,
                             str(sandbox_env), str(mount)], capture_output=True, text=True)
    if result.returncode == 77 or 'Operation not permitted' in result.stderr:
        pytest.skip('本机不支持用户命名空间内测试挂载')
    assert result.returncode == 0, result.stderr
