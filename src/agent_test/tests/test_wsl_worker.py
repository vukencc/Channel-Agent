import json
import os
from pathlib import Path
import subprocess
import sys
import queue
import shutil
import threading

import pytest

WORKER = Path(__file__).parents[2] / 'agent_core' / 'tools' / 'wsl_command_worker.py'


def test_worker_rejects_unknown_operation(tmp_path):
    request = {'id': 'test', 'operation': 'unknown', 'arguments': {}, 'root': str(tmp_path),
               'config': {}, 'policy': 'readonly', 'execution_environment': 'sandbox'}
    proc = subprocess.run([sys.executable, str(WORKER)], input=json.dumps(request) + '\n',
                          text=True, capture_output=True, timeout=15, env=os.environ.copy())
    assert proc.returncode != 0
    assert json.loads(proc.stdout)['type'] == 'error'



def _start_worker(tmp_path, *, acknowledge_audit=True, **updates):
    request = {'id': 'test', 'operation': 'run_command', 'arguments': {'command': 'echo worker'},
               'root': str(tmp_path), 'config': {}, 'policy': 'smart',
               'execution_environment': 'sandbox'}
    request.update(updates)
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    proc = subprocess.Popen([sys.executable, str(WORKER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    proc.stdin.write(json.dumps(request) + '\n')
    proc.stdin.flush()
    proc.worker_events = queue.Queue()

    def receive():
        for line in proc.stdout:
            event = json.loads(line)
            if event['type'] == 'audit' and acknowledge_audit:
                proc.stdin.write(json.dumps({'id': 'test', 'type': 'audit_ack',
                                             'event_id': event['event_id']}) + '\n')
                proc.stdin.flush()
            proc.worker_events.put(event)
    threading.Thread(target=receive, daemon=True).start()
    return proc


@pytest.mark.skipif(sys.platform != 'linux', reason='需要 Linux Bubblewrap worker')
def test_worker_waits_for_persistent_audit_before_side_effect(tmp_path):
    proc = _start_worker(tmp_path, acknowledge_audit=False, policy='full_access',
                         arguments={'command': 'touch changed'})
    try:
        assert _read(proc)['type'] == 'ready'
        event = _read(proc)
        assert event['type'] == 'audit' and event['entry']['event'] in {'confirm', 'resource_limits'}
        assert not (tmp_path / 'changed').exists()
        proc.stdin.write(json.dumps({'id': 'test', 'type': 'cancel'}) + '\n')
        while event['type'] not in {'result', 'error'}:
            if event['type'] == 'audit':
                proc.stdin.write(json.dumps({'id': 'test', 'type': 'audit_ack', 'event_id': event['event_id']}) + '\n')
            proc.stdin.flush()
            event = _read(proc)
        assert event['type'] == 'result'
        assert not (tmp_path / 'changed').exists()
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def _read(proc):
    return proc.worker_events.get(timeout=15)


def _confirmation(proc):
    event = _read(proc)
    while event['type'] == 'audit':
        event = _read(proc)
    assert event['type'] == 'confirm', event
    return event


@pytest.mark.skipif(sys.platform != 'linux', reason='验证 Linux 依赖与 Windows 包路径隔离')
def test_wheel_layout_does_not_import_windows_dependency_siblings(tmp_path, monkeypatch):
    package = tmp_path / 'windows-site' / 'agent_core'
    shutil.copytree(WORKER.parents[1], package, ignore=shutil.ignore_patterns('__pycache__'))
    (package.parent / 'pydantic.py').write_text('raise RuntimeError("Windows dependency must not load")')
    monkeypatch.setitem(globals(), 'WORKER', package / 'tools' / 'wsl_command_worker.py')
    root = tmp_path / 'work'
    root.mkdir()
    proc = _start_worker(root, policy='full_access')
    try:
        assert _read(proc)['type'] == 'ready'
        while True:
            event = _read(proc)
            if event['type'] in {'result', 'error'}:
                break
        assert event['type'] == 'result', event
        assert '[退出码] 0' in event['result'] and 'worker' in event['result']
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        proc.communicate(timeout=10)


@pytest.mark.skipif(sys.platform != 'linux', reason='验证真实 worker 管道中断')
def test_broken_audit_pipe_prevents_command_start(tmp_path):
    request = {'id': 'broken-pipe', 'operation': 'run_command',
        'arguments': {'command': 'touch must-not-execute'}, 'root': str(tmp_path),
        'config': {}, 'policy': 'full_access', 'execution_environment': 'sandbox'}
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    proc = subprocess.Popen([sys.executable, str(WORKER)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        proc.stdin.write(json.dumps(request) + '\n')
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())['type'] == 'ready'
        while True:
            event = json.loads(proc.stdout.readline())
            assert event['type'] == 'audit', event
            last = event['entry']['event'] == 'confirm'
            if last:
                proc.stdout.close()
            proc.stdin.write(json.dumps({'id': request['id'], 'type': 'audit_ack',
                                        'event_id': event['event_id']}) + '\n')
            proc.stdin.flush()
            if last:
                break
        assert proc.wait(timeout=10) != 0
        assert not (tmp_path / 'must-not-execute').exists()
    finally:
        proc.kill()
        proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.skipif(sys.platform != "linux", reason="需要 Linux Bubblewrap worker")
def test_confirmation_is_relayed_and_denial_is_audited(tmp_path):
    proc = _start_worker(tmp_path, arguments={'command': 'echo worker', 'resource_profile': 'development'})
    try:
        assert _read(proc)['type'] == 'ready'
        event = _confirmation(proc)
        proc.stdin.write(json.dumps({'id': 'test', 'type': 'decision',
                                     'request_id': event['request_id'], 'allowed': False}) + '\n')
        proc.stdin.flush()
        events = []
        while True:
            event = _read(proc)
            events.append(event)
            if event['type'] in {'result', 'error'}:
                break
        assert event['type'] == 'result'
        assert '[已取消]' in event['result']
        assert any(e['type'] == 'audit' and e['entry']['event'] == 'confirm' for e in events)
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        proc.communicate(timeout=10)


@pytest.mark.skipif(sys.platform != "linux", reason="需要 Linux Bubblewrap worker")
def test_cancel_while_confirmation_pending(tmp_path):
    proc = _start_worker(tmp_path, arguments={'command': 'echo worker', 'resource_profile': 'development'})
    try:
        assert _read(proc)['type'] == 'ready'
        _confirmation(proc)
        proc.stdin.write(json.dumps({'id': 'test', 'type': 'cancel'}) + '\n')
        proc.stdin.flush()
        while True:
            event = _read(proc)
            if event['type'] in {'result', 'error'}:
                break
        assert event['type'] == 'result'
        assert '[已取消]' in event['result']
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        proc.communicate(timeout=10)


@pytest.mark.skipif(sys.platform != "linux", reason="需要 Linux Bubblewrap worker")
def test_cancel_running_command_reaps_children(tmp_path):
    import time
    proc = _start_worker(tmp_path, policy='full_access', arguments={
        'command': 'touch started; (sleep 2; touch late) & wait'})
    try:
        assert _read(proc)['type'] == 'ready'
        deadline = time.monotonic() + 10
        while not (tmp_path / 'started').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (tmp_path / 'started').exists(), 'sandbox command did not start'
        proc.stdin.write(json.dumps({'id': 'test', 'type': 'cancel'}) + '\n')
        proc.stdin.flush()
        while True:
            event = _read(proc)
            if event['type'] in {'result', 'error'}:
                break
        assert event['type'] == 'result'
        assert '[已取消]' in event['result']
        assert proc.wait(timeout=10) == 0
        time.sleep(2.2)
        assert not (tmp_path / 'late').exists()
    finally:
        proc.kill()
        proc.communicate(timeout=10)
