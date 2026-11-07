"""原生 Windows 到真实 WSL/Bubblewrap 的边界验证。"""
import json
import os
from pathlib import Path
import threading
import time
import subprocess

import pytest

from agent_core import config
from agent_core.tools.command import run_command
from agent_core.tools.sandbox import ToolContext, tool_context


pytestmark = pytest.mark.skipif(os.name != 'nt' or os.getenv('RUN_WINDOWS_WSL_TESTS') != '1',
                                reason='需要显式启用真实 Windows 与 WSL 桥接测试')


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'WINDOWS_WSL_PYTHON', os.environ['TEST_WINDOWS_WSL_PYTHON'])
    monkeypatch.setattr(config, 'COMMAND_TIMEOUT', 12.0)
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {})
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'off')
    root = tmp_path / '中文 project'
    root.mkdir()
    cancelled = threading.Event()
    context = ToolContext(root, tmp_path / 'audit.jsonl', lambda *_: True, cancelled,
                          permission_policy='full_access', execution_environment='sandbox',
                          root_identity=(root.stat().st_dev, root.stat().st_ino), flow_workspace_scope=True)
    return root, context


def test_actual_wsl_command_uses_selected_project_and_isolation(bridge, tmp_path):
    root, context = bridge
    sentinel = tmp_path / 'outside.txt'
    sentinel.write_text('protected', encoding='utf-8')
    with tool_context(context):
        result = run_command("printf bridge-ok > result.txt; pwd; cat result.txt; test ! -e /mnt/d/AI/AgentSrcCode/ChannelAgent/pyproject.toml")
    assert 'bridge-ok' in result and '/workspace' in result and '[退出码] 0' in result, result
    assert (root / 'result.txt').read_text() == 'bridge-ok'
    assert sentinel.read_text() == 'protected'
    entries = [json.loads(line) for line in context.audit_path.read_text(encoding='utf-8').splitlines()]
    assert any(row['event'] == 'wsl_tool_audit' for row in entries)


def test_actual_wsl_confirmation_denial_does_not_write(bridge):
    root, context = bridge
    context.permission_policy = 'standard'
    context.confirm = lambda *_: False
    with tool_context(context):
        result = run_command('touch denied.txt')
    assert '取消' in result or '未确认' in result, result
    assert not (root / 'denied.txt').exists()


def test_actual_wsl_audit_failure_blocks_side_effect(bridge, monkeypatch):
    from agent_core.tools import sandbox
    root, context = bridge
    append = sandbox.append_audit

    def disk_full(path, line, sync=False):
        if json.loads(line).get('event') == 'wsl_tool_audit':
            raise OSError('synthetic disk full')
        return append(path, line, sync=sync)

    monkeypatch.setattr(sandbox, 'append_audit', disk_full)
    with tool_context(context):
        result = run_command('touch must-not-execute')
    assert '拦截' in result, result
    assert not (root / 'must-not-execute').exists()


def test_actual_wsl_flow_rejects_external_hardlink(bridge, tmp_path):
    root, context = bridge
    outside = tmp_path / 'outside.txt'
    outside.write_text('protected', encoding='utf-8')
    os.link(outside, root / 'linked.txt')
    with tool_context(context):
        result = run_command('printf changed > linked.txt')
    assert '拦截' in result, result
    assert outside.read_text() == 'protected'


def test_actual_wsl_flow_cannot_follow_native_junction(bridge, tmp_path):
    root, context = bridge
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel.txt'
    sentinel.write_text('protected', encoding='utf-8')
    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(root / 'escape'), str(outside)],
                   check=True, capture_output=True)
    with tool_context(context):
        result = run_command('printf changed > escape/sentinel.txt')
    assert sentinel.read_text() == 'protected', result
    assert '[退出码] 0' not in result


def test_actual_wsl_cancellation_drains_descendants(bridge):
    root, context = bridge
    started = threading.Event()
    audit_before_marker = []
    def cancel_after_start():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (root / 'started').exists():
                entries = [json.loads(line) for line in context.audit_path.read_text(encoding='utf-8').splitlines()]
                audit_before_marker.append(any(row.get('worker_event', {}).get('event') == 'command_started' for row in entries))
                started.set()
                break
            time.sleep(.02)
        context.cancelled.set()
    controller = threading.Thread(target=cancel_after_start)
    with tool_context(context):
        controller.start()
        try:
            result = run_command('touch started; (sleep 2; touch late) & wait')
        finally:
            controller.join()
    assert '取消' in result, result
    assert started.is_set()
    assert audit_before_marker == [True]
    time.sleep(3)
    assert not (root / 'late').exists()
