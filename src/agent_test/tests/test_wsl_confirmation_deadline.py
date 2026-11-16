"""真实 worker 的确认协议，不代表 Windows 原生端到端。"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_core.tools import wsl_command
from agent_core.tools import sandbox


@pytest.mark.skipif(sys.platform != 'linux', reason='需要 Linux worker')
def test_worker_delayed_host_rejection_keeps_audit_channel(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    request = {'id': 'test', 'operation': 'run_command',
               'arguments': {'command': 'python3 -c "open(\'marker\',\'w\').write(\'x\')"'},
               'root': str(root), 'config': {'CONFIRM_TIMEOUT': .05, 'COMMAND_TIMEOUT': 2},
               'policy': 'smart', 'paths': {}, 'execution_environment': 'sandbox'}
    env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1'}
    process = subprocess.Popen([sys.executable, '-u', str(Path(wsl_command.__file__).with_name('wsl_command_worker.py'))],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env)
    records = queue.Queue()
    threading.Thread(target=lambda: [records.put(json.loads(line)) for line in process.stdout], daemon=True).start()
    def send(value):
        process.stdin.write(json.dumps({'id': 'test', **value}) + '\n')
        process.stdin.flush()
    try:
        process.stdin.write(json.dumps(request) + '\n')
        process.stdin.flush()
        events = []
        while True:
            record = records.get(timeout=5)
            events.append(record)
            if record['type'] == 'confirm':
                time.sleep(.2)
                send({'type': 'decision', 'request_id': record['request_id'], 'allowed': False})
            elif record['type'] == 'audit':
                send({'type': 'audit_ack', 'event_id': record['event_id']})
            elif record['type'] in ('result', 'error'):
                break
        assert events[-1]['type'] == 'result' and '已取消' in events[-1]['result']
        assert any(event.get('entry', {}).get('event') == 'confirm' and event['entry']['allowed'] is False for event in events)
        assert not any(event.get('entry', {}).get('event') == 'command_started' for event in events)
        assert not (root / 'marker').exists()
    finally:
        process.stdin.close()
        process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != 'linux', reason='需要 Linux worker')
@pytest.mark.parametrize('mode', ['cancel', 'eof', 'total_deadline', 'unknown_positive', 'non_boolean', 'late_positive'])
def test_worker_confirmation_stops_on_cancel_eof_deadline_or_invalid_decision(tmp_path, mode):
    root = tmp_path / 'workspace'
    root.mkdir()
    worker = str(Path(wsl_command.__file__).with_name('wsl_command_worker.py'))
    if mode == 'total_deadline':
        script = ('import runpy,threading,sys; original=threading.Timer; '
                  'threading.Timer=lambda interval,fn: original(.3,fn); '
                  'runpy.run_path(sys.argv[1],run_name="__main__")')
        argv = [sys.executable, '-u', '-c', script, worker]
    else:
        argv = [sys.executable, '-u', worker]
    request = {'id': 'test', 'operation': 'run_command',
               'arguments': {'command': 'python3 -c "open(\'marker\',\'w\').write(\'x\')"'},
               'root': str(root), 'config': {'CONFIRM_TIMEOUT': .05, 'COMMAND_TIMEOUT': 2},
               'policy': 'smart', 'paths': {}, 'execution_environment': 'sandbox'}
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'})
    records = queue.Queue()
    threading.Thread(target=lambda: [records.put(json.loads(line)) for line in process.stdout], daemon=True).start()
    def send(value):
        process.stdin.write(json.dumps({'id': 'test', **value}) + '\n')
        process.stdin.flush()
    try:
        process.stdin.write(json.dumps(request) + '\n')
        process.stdin.flush()
        events = []
        while True:
            record = records.get(timeout=20)
            events.append(record)
            if record['type'] == 'confirm':
                if mode == 'cancel':
                    send({'type': 'cancel'})
                elif mode == 'eof':
                    process.stdin.close()
                elif mode == 'unknown_positive':
                    send({'type': 'decision', 'request_id': 'unknown', 'allowed': True})
                elif mode == 'non_boolean':
                    send({'type': 'decision', 'request_id': record['request_id'], 'allowed': 'true'})
                elif mode == 'late_positive':
                    send({'type': 'decision', 'request_id': record['request_id'], 'allowed': False})
                    send({'type': 'decision', 'request_id': record['request_id'], 'allowed': True})
            elif record['type'] == 'audit' and not process.stdin.closed and mode not in ('cancel', 'total_deadline'):
                send({'type': 'audit_ack', 'event_id': record['event_id']})
            elif record['type'] in ('result', 'error'):
                break
        assert not any(event.get('entry', {}).get('event') == 'command_started' for event in events)
        assert not (root / 'marker').exists()
        if mode in ('cancel', 'total_deadline'):
            assert events[-1]['type'] == 'error'
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != 'linux', reason='需要 Linux worker')
def test_host_timeout_decision_rejects_and_drains(tmp_path, sandbox_env):
    worker = str(Path(wsl_command.__file__).with_name('wsl_command_worker.py'))
    request = {'id': 'host-timeout', 'operation': 'run_command',
               'arguments': {'command': 'python3 -c "open(\'marker\',\'w\').write(\'x\')"'},
               'root': str(sandbox_env), 'config': {'CONFIRM_TIMEOUT': .05, 'COMMAND_TIMEOUT': 2},
               'policy': 'smart', 'paths': {}, 'execution_environment': 'sandbox'}
    argv = [sys.executable, '-u', worker]
    waits = []
    def host_confirm(prompt, timeout):
        waits.append(timeout)
        time.sleep(timeout)
        return False
    stat = sandbox_env.stat()
    context = sandbox.ToolContext(sandbox_env, tmp_path / 'audit.jsonl', host_confirm, threading.Event(),
                                  permission_policy='smart', root_identity=(stat.st_dev, stat.st_ino),
                                  execution_environment='sandbox')
    with sandbox.tool_context(context):
        result = wsl_command._exchange(argv, request, context.cancelled)
    assert waits == [.05] and '已取消' in result
    assert not (sandbox_env / 'marker').exists()
    entries = [json.loads(line).get('worker_event', {}) for line in context.audit_path.read_text().splitlines()]
    assert any(entry.get('event') == 'confirm' and entry.get('allowed') is False for entry in entries)
    assert not any(entry.get('event') == 'command_started' for entry in entries)
