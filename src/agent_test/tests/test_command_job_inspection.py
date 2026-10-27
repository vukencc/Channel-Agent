"""后台命令合并检查保留归属、完整 JSON 与真实审计。"""
import json
import threading

import pytest

from agent_core import config
from agent_core.core.command_jobs import CommandJobs
from agent_core.core.storage import SessionStore
from agent_core.tools import TOOL_REGISTRY
from agent_core.tools.command_jobs import register_command_jobs
from agent_core.tools.sandbox import ToolContext, tool_context


@pytest.fixture
def inspection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 420)
    previous = dict(TOOL_REGISTRY)
    register_command_jobs()
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    jobs = CommandJobs(store)
    identifier, owner = 'a' * 32, 'b' * 32
    jobs.records[identifier] = {'id': identifier, 'owner': owner, 'status': 'completed',
        'execution_environment': 'sandbox', 'runtime_profile': 'system'}
    log = jobs.directory / (identifier + '.log')
    log.write_text('过去输出' * 300 + '"\\\n末尾' * 80)
    context = ToolContext(tmp_path, tmp_path / 'audit.jsonl', lambda *_: False,
                          threading.Event(), session_id=owner, job_manager=jobs)
    try:
        yield jobs, context, identifier, log
    finally:
        jobs.close()
        store.close()
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(previous)


def inspect(context, identifier):
    with tool_context(context):
        return TOOL_REGISTRY['inspect_command_job'].run(json.dumps({'job_id': identifier}))


def test_inspection_returns_tail_and_status_in_bounded_valid_json_with_metadata_audit(inspection):
    jobs, context, identifier, log = inspection
    raw = inspect(context, identifier)
    result = json.loads(raw)
    assert len(raw) <= config.TOOL_MAX_OUTPUT
    assert result['job_id'] == identifier and result['status'] == 'completed'
    assert result['drained'] is True
    assert result['log_truncated'] is True
    assert log.read_text().endswith(result['log_tail']) and result['log_tail']
    rows = [json.loads(line) for line in context.audit_path.read_text().splitlines()]
    event = next(row for row in rows if row['event'] == 'command_job_inspected')
    assert event['job_id'] == identifier and event['status'] == 'completed'
    assert '末尾' not in context.audit_path.read_text()


def test_inspection_denies_unrelated_owner_before_opening_logs(inspection):
    jobs, context, identifier, log = inspection
    context.session_id = 'c' * 32
    log.unlink()
    log.symlink_to(log.parent / 'unrelated-private-log')
    with pytest.raises(PermissionError):
        inspect(context, identifier)


def test_inspection_refuses_symlink_logs_and_too_small_metadata_budget(inspection, monkeypatch):
    jobs, context, identifier, log = inspection
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 20)
    with pytest.raises(ValueError, match='TOOL_MAX_OUTPUT'):
        inspect(context, identifier)
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 420)
    log.unlink()
    log.symlink_to(log.parent / 'other')
    with pytest.raises(OSError):
        inspect(context, identifier)


def test_inspection_does_not_report_drained_before_final_worker_audit(inspection):
    from concurrent.futures import Future
    jobs, context, identifier, log = inspection
    jobs.futures[identifier] = Future()
    assert not json.loads(inspect(context, identifier))['drained']
    jobs.futures[identifier].set_result(None)
    assert json.loads(inspect(context, identifier))['drained']


@pytest.mark.parametrize('maximum', [300, 350, 420, 512, 1000])
@pytest.mark.parametrize('text', ['结果', '"\\\n中文' * 200, ''])
def test_inspection_budget_counts_escaping_metadata_and_both_text_fields(inspection, monkeypatch,
                                                                       maximum, text):
    jobs, context, identifier, log = inspection
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', maximum)
    jobs.records[identifier]['result'] = text
    log.write_text(text)
    raw = inspect(context, identifier)
    result = json.loads(raw)
    assert len(raw) <= maximum
    assert text.startswith(result['result'])
    assert text.endswith(result['log_tail'])
    assert result['result_truncated'] == (result['result'] != text)
    assert result['log_truncated'] == (result['log_tail'] != text)


def test_inspection_capture_limit_flag_describes_retained_log_not_unseen_output(inspection, monkeypatch):
    jobs, context, identifier, log = inspection
    monkeypatch.setattr(config, 'COMMAND_JOB_LOG_BYTES', 4)
    log.write_bytes(b'1234')
    result = json.loads(inspect(context, identifier))
    assert result['log_tail'] == '1234'
    assert result['log_capture_limit_reached'] is True
    assert result['log_truncated'] is False
