"""Background jobs retain the explicitly approved sandbox runtime profile."""
from contextlib import nullcontext
import json
from pathlib import Path
import shlex
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError

from ai_agent_startup import config
from ai_agent_startup.core import command_jobs
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import command, network_proxy
from ai_agent_startup.tools.command_jobs import StartCommandJobArgs, start_command_job
from ai_agent_startup.tools.sandbox import SandboxError, ToolContext, tool_context


@pytest.fixture
def jobs_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', True)
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 0)
    workspace = tmp_path / uuid.uuid4().hex
    workspace.mkdir()
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    jobs = command_jobs.CommandJobs(store)
    confirmations = []
    context = ToolContext(workspace, tmp_path / 'audit.jsonl',
                          lambda prompt, timeout: confirmations.append(prompt) or True,
                          threading.Event(), job_manager=jobs, permission_policy='standard')
    try:
        yield jobs, context, confirmations, store
    finally:
        jobs.close()
        store.close()


def _wait_done(jobs, context, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = jobs.status(context.root.name, identifier)
        if record['status'] not in {'queued', 'running'}:
            return record
        time.sleep(.01)
    pytest.fail('Background runtime job did not finish')


def test_job_tool_schema_limits_runtime_profile_to_system_or_node():
    assert StartCommandJobArgs(command='true').runtime_profile == 'system'
    assert StartCommandJobArgs(command='node --version', runtime_profile='node').runtime_profile == 'node'
    with pytest.raises(ValidationError):
        StartCommandJobArgs(command='true', runtime_profile='host')


def test_node_profile_disabled_fails_before_confirmation_or_queue(jobs_env):
    jobs, context, confirmations, _ = jobs_env
    with tool_context(context), pytest.raises(SandboxError, match='COMMAND_NODE_MEMORY_MB'):
        start_command_job('touch must-not-run', runtime_profile='node')
    assert confirmations == []
    assert jobs.records == {}
    assert not (context.root / 'must-not-run').exists()


def test_default_job_preserves_system_profile_and_persists_it(jobs_env):
    jobs, context, confirmations, _ = jobs_env
    with tool_context(context):
        identifier = start_command_job('printf system-job', timeout=2)
    record = _wait_done(jobs, context, identifier)
    jobs.close()
    assert record['status'] == 'completed'
    assert record['runtime_profile'] == 'system'
    assert jobs.logs(context.root.name, identifier) == 'system-job'
    saved = json.loads((jobs.directory / (identifier + '.json')).read_text())
    assert saved['runtime_profile'] == 'system'
    assert 'system' in confirmations[0] and f'{config.COMMAND_MEMORY_MB} MiB' in confirmations[0]


def test_real_node_background_profile_executes_and_persists_logs(jobs_env, monkeypatch):
    prefixes = sorted((Path.home() / '.nvm/versions/node').glob('v24.*/bin/node'), reverse=True)
    if not prefixes:
        pytest.skip('A local Linux Node 24 distribution is required for the execution regression')
    jobs, context, confirmations, store = jobs_env
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': str(prefixes[0].parent.parent)})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048)
    monkeypatch.setenv('BACKGROUND_NODE_SECRET', 'host-private')
    script = ('if (6 * 7 !== 42 || process.env.BACKGROUND_NODE_SECRET !== undefined) '
              'process.exit(9); console.log("node-background")')
    with tool_context(context):
        identifier = start_command_job('node -e ' + shlex.quote(script), timeout=3,
                                       runtime_profile='node')
    record = _wait_done(jobs, context, identifier)
    jobs.close()
    assert record['status'] == 'completed', record['result']
    assert record['runtime_profile'] == 'node'
    assert jobs.logs(context.root.name, identifier).strip() == 'node-background'
    assert 'node' in confirmations[0] and '2048 MiB' in confirmations[0]
    audit = [json.loads(line) for line in context.audit_path.read_text().splitlines()]
    for event in ('command_job_queued', 'command_job_finished'):
        assert any(row.get('event') == event and row.get('runtime_profile') == 'node' for row in audit)
    restarted = command_jobs.CommandJobs(store)
    try:
        assert restarted.status(context.root.name, identifier)['runtime_profile'] == 'node'
        assert restarted.logs(context.root.name, identifier).strip() == 'node-background'
    finally:
        restarted.close()


def test_node_profile_is_forwarded_to_network_execution(jobs_env, monkeypatch, tmp_path):
    jobs, context, _, _ = jobs_env
    monkeypatch.setattr(config, 'COMMAND_RUNTIME_PATHS', {'node': '/explicit/runtime'})
    monkeypatch.setattr(config, 'COMMAND_NODE_MEMORY_MB', 2048)
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'allowlist')
    calls = []
    monkeypatch.setattr(command_jobs, 'isolated_command',
                        lambda argv, **kwargs: calls.append(kwargs) or argv)
    monkeypatch.setattr(network_proxy, 'validate_network_config', lambda: None)
    proxy = SimpleNamespace(path=tmp_path / 'proxy.sock')
    monkeypatch.setattr(network_proxy, 'NetworkProxy', lambda: nullcontext(proxy))
    monkeypatch.setattr(command, '_execute', lambda *args, **kwargs: '[退出码] 0\nnetwork-node')
    with tool_context(context):
        identifier = jobs.start('node -e "console.log(42)"', network=True, timeout=2,
                                runtime_profile='node')
    assert _wait_done(jobs, context, identifier)['status'] == 'completed'
    assert calls == [{'runtime_profile': 'node'},
                     {'network_socket': proxy.path, 'runtime_profile': 'node'}]


def test_legacy_record_without_profile_uses_system_in_worker(jobs_env, monkeypatch):
    jobs, context, _, _ = jobs_env
    identifier = uuid.uuid4().hex
    jobs.records[identifier] = {'id': identifier, 'owner': context.root.name, 'command': 'printf legacy',
                                'network': False, 'timeout': 2, 'status': 'queued', 'result': ''}
    profiles = []
    monkeypatch.setattr(command_jobs, 'isolated_command',
                        lambda argv, **kwargs: profiles.append(kwargs.get('runtime_profile')) or argv)
    monkeypatch.setattr(command, '_execute', lambda *args, **kwargs: '[退出码] 0\nlegacy')
    jobs._run(identifier, context, '')
    assert jobs.status(context.root.name, identifier)['status'] == 'completed'
    assert profiles == ['system']
