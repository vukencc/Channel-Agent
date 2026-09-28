import json
import threading
import time
import uuid

import pytest

import config
from core.storage import SessionStore
from tools.sandbox import ToolContext, tool_context


def setup_jobs(tmp_path, monkeypatch):
    from core.command_jobs import CommandJobs
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', True, raising=False)
    root = tmp_path / uuid.uuid4().hex
    root.mkdir()
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    jobs = CommandJobs(store)
    context = ToolContext(root, tmp_path / 'audit.jsonl', lambda *_: True, threading.Event(), job_manager=jobs)
    return jobs, context, store


def wait_done(jobs, context, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = jobs.status(context.root.name, identifier)
        if value['status'] not in {'queued', 'running'}:
            return value
        time.sleep(.02)
    pytest.fail('后台任务未结束')


def test_job_runs_real_sandbox_and_foreground_writes_wait(tmp_path, monkeypatch):
    jobs, context, store = setup_jobs(tmp_path, monkeypatch)
    try:
        with tool_context(context):
            identifier = jobs.start('printf begin; sleep .3; printf end', '', False, 2)
        deadline = time.monotonic() + 2
        while jobs.status(context.root.name, identifier)['status'] == 'queued' and time.monotonic() < deadline:
            time.sleep(.01)
        with pytest.raises(PermissionError):
            jobs.status(uuid.uuid4().hex, identifier)
        start = time.monotonic()
        with jobs.guard(context.root, threading.Event()):
            assert time.monotonic() - start >= .15
        result = wait_done(jobs, context, identifier)
        assert result['status'] == 'completed'
        assert jobs.logs(context.root.name, identifier) == 'beginend'
        assert json.loads((jobs.directory / (identifier + '.json')).read_text())['status'] == 'completed'
    finally:
        jobs.close()
        store.close()


def test_cancel_job_kills_children_and_restart_does_not_replay(tmp_path, monkeypatch):
    jobs, context, store = setup_jobs(tmp_path, monkeypatch)
    try:
        with tool_context(context):
            identifier = jobs.start('sleep 2; touch late', '', False, 3)
        jobs.cancel(context.root.name, identifier)
        assert wait_done(jobs, context, identifier)['status'] == 'cancelled'
        assert not (context.root / 'late').exists()
        jobs.close()
        path = jobs.directory / (identifier + '.json')
        record = json.loads(path.read_text())
        record['status'] = 'running'
        path.write_text(json.dumps(record))
        from core.command_jobs import CommandJobs
        jobs = CommandJobs(store)
        assert jobs.status(context.root.name, identifier)['status'] == 'interrupted'
        assert not (context.root / 'late').exists()
    finally:
        jobs.close()
        store.close()


def test_jobs_deny_confirmation_and_apply_timeout_output_cap(tmp_path, monkeypatch):
    jobs, context, store = setup_jobs(tmp_path, monkeypatch)
    try:
        context.confirm = lambda *_: False
        with tool_context(context), pytest.raises(PermissionError):
            jobs.start('touch denied', '', False, 1)
        assert not (context.root / 'denied').exists()
        monkeypatch.setattr(config, 'COMMAND_JOB_LOG_BYTES', 32)
        context.confirm = lambda *_: True
        with tool_context(context):
            identifier = jobs.start('printf "%0100d" 0; sleep 3', '', False, .2)
        assert wait_done(jobs, context, identifier)['status'] == 'timeout'
        assert len(jobs.logs(context.root.name, identifier).encode()) == 32
    finally:
        jobs.close()
        store.close()
