"""工程验证脚本的隔离边界与恢复后状态新鲜度。"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


@pytest.mark.parametrize('external_code,own_code,expected_status', [
    (1, 0, 1),
    (0, 0, 0),
])
def test_workboard_acceptance_exit_reflects_both_check_results(
        tmp_path, monkeypatch, external_code, own_code, expected_status):
    path = Path(__file__).parents[1] / 'scripts' / 'workboard_acceptance.py'
    spec = importlib.util.spec_from_file_location('workboard_acceptance_status', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = tmp_path / 'agent-project-validation-acceptance'
    workspace = root / 'work' / 'generated-project'
    workspace.mkdir(parents=True)
    from ai_agent_startup.tools import command
    calls = []

    def fake_run_command(script, *, reason):
        calls.append(script)
        code = own_code if 'unittest discover' in script else external_code
        return f'[退出码] {code}\nfixture verification output'

    monkeypatch.setattr(command, 'run_command', fake_run_command)
    monkeypatch.setattr(sys, 'argv', ['workboard_acceptance.py', str(root)])
    try:
        result = module.main()
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else 1
    else:
        status = 0 if result is None else result
    assert status == expected_status
    assert len(calls) == 2
    report = (root / 'acceptance.txt').read_text(encoding='utf-8')
    assert f'[退出码] {external_code}' in report
    assert f'[退出码] {own_code}' in report


def test_public_diagnostics_keep_configuration_names_without_credentials():
    from ai_agent_startup.web.context_status import public_run_error
    output = public_run_error('ValueError: BASE_URL contains U+000A in private-token\n.example')
    assert 'BASE_URL' in output and 'U+000A' in output
    assert 'private-token' not in output and '.example' not in output
    assert 'private-key' not in public_run_error('ProviderError: private-key')


def test_experiment_rejects_lexical_escape_before_creating_files(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / 'scripts' / 'project_validation.py'
    spec = importlib.util.spec_from_file_location('validation_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.tempfile, 'gettempdir', lambda: str(tmp_path / 'allowed'))
    monkeypatch.setattr(sys, 'argv', ['project_validation.py', '--root',
                        str(tmp_path / 'allowed' / '..' / 'agent-project-validation-escape')])
    import pytest
    with pytest.raises(SystemExit) as raised:
        module.main()
    assert raised.value.code == 2
    assert not (tmp_path / 'agent-project-validation-escape').exists()


def test_experiment_rebinds_every_mutable_root_including_maintenance(tmp_path):
    path = Path(__file__).parents[1] / 'scripts' / 'project_validation.py'
    spec = importlib.util.spec_from_file_location('validation_isolation', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = SimpleNamespace(AUDIT_LOG=Path('/real/logs/audit.log'),
        RAG_CACHE_DIR=Path('/real/.cache/rag'), AGENT_STATE_DIR=Path('/real/.agent'),
        SANDBOX_DIR=Path('/real/crud_tests'), DOC_DIR=Path('/real/data/raw'),
        TOOL_ROOTS={'real': {}}, RAG_SOURCES={'real': {}})
    module.isolate_experiment(settings, tmp_path)
    for name in ('AUDIT_LOG', 'RAG_CACHE_DIR', 'AGENT_STATE_DIR', 'SANDBOX_DIR', 'DOC_DIR'):
        assert getattr(settings, name).is_relative_to(tmp_path)
    assert not settings.TOOL_ROOTS and not settings.RAG_SOURCES


def test_experiment_web_cleanup_changes_only_temporary_paths(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from ai_agent_startup.web.app import create_app
    path = Path(__file__).parents[1] / 'scripts' / 'project_validation.py'
    spec = importlib.util.spec_from_file_location('validation_cleanup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ('AUDIT_LOG', 'RAG_CACHE_DIR', 'AGENT_STATE_DIR', 'SANDBOX_DIR',
                 'DOC_DIR', 'TOOL_ROOTS', 'RAG_SOURCES'):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    untouched = tmp_path / 'outside.log'
    untouched.write_text('must remain unchanged')
    module.isolate_experiment(config, experiment)
    config.RAG_CACHE_DIR.mkdir()
    (config.RAG_CACHE_DIR / 'generated').write_text('disposable cache')
    config.AUDIT_LOG.write_text('disposable log')
    store = SessionStore(config.AGENT_STATE_DIR, config.SANDBOX_DIR)
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            result = client.post('/api/maintenance/cleanup', json={
                'cache': True, 'logs': True, 'sessions': False, 'confirm': True})
            assert result.status_code == 200, result.text
            assert not (config.RAG_CACHE_DIR / 'generated').exists()
            assert untouched.read_text() == 'must remain unchanged'
            assert config.AUDIT_LOG.is_relative_to(experiment)
    finally:
        store.close()


def test_normal_round_clears_metrics_from_previous_model_recovery(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True)
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', tmp_path / 'supervisor.env')
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 1)

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        calls = 0
        preparations = 0
        snapshots = []

        async def model(history, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError('intentional regression fixture')
            if calls == 2:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'read1', 'type': 'function', 'function': {
                        'name': 'read_file', 'arguments': '{"path":"example.txt"}'}}]}
            snapshots.append(dict(session.record['last_run']))
            return {'role': 'assistant', 'content': 'finished'}

        manager = SessionManager(store, model=model)
        session = manager.create()

        async def prepare(owner, history, **kwargs):
            nonlocal preparations
            preparations += 1
            return history, {'structured': True, 'sent_tokens': preparations * 100,
                             'utilization': preparations / 10}

        async def tool(*args, **kwargs):
            return 'isolated fixture result'

        monkeypatch.setattr(manager.history_context, 'prepare', prepare)
        monkeypatch.setattr(manager, '_tool', tool)
        try:
            manager.submit(session, 'Run a short task')
            await session.task
            assert calls == 3
            assert snapshots[0]['context']['sent_tokens'] == 300
            assert 'recovery_context' not in snapshots[0]
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())


def test_plan_runtime_links_real_tool_fact_to_latest_revision(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        count = 0

        async def model(history, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'create1', 'type': 'function', 'function': {'name': 'create_file',
                    'arguments': json.dumps({'path': 'completed.txt', 'content': 'actual fixture file'})}}]}
            return {'role': 'assistant', 'content': 'one operation finished'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.permission_override = 'full_access'
        try:
            plan = await manager.task_plans.create(session, 'Write a file', [{
                'key': 'write', 'title': 'Write', 'acceptance': 'file exists'}], expected_revision=0)
            await manager.task_plans.update(session, plan['plan_id'], 'write', 'in_progress',
                                            expected_revision=plan['revision'])
            manager.submit(session, 'Write completed.txt')
            await session.task
            assert (manager.workspace(session) / 'completed.txt').read_text() == 'actual fixture file'
            summary = await manager.task_plans.summary(session)
            data = json.loads(summary[summary.index('{'):])
            evidence = manager.task_plans.cache[session.id]['evidence'][-1]
            assert data['revision'] == manager.task_plans.cache[session.id]['revision']
            assert data['recent_evidence'][0]['id'] == evidence['id']
            assert data['recent_evidence'][0]['source'] == 'executed'
            assert data['recent_evidence'][0]['name'] == 'create_file'
            assert 'completed.txt' in data['recent_evidence'][0]['summary']
            assert len(summary) <= config.TASK_PLAN_CONTEXT_CHARS
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())


def test_repeated_real_reads_prompt_progress_and_do_not_cross_turns(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        count = 0
        windows = []

        async def model(history, **kwargs):
            nonlocal count
            count += 1
            windows.append(history[0]['content'])
            if count <= 3:
                return {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': f'read{count}', 'type': 'function', 'function': {
                        'name': 'read_file', 'arguments': '{"path":"unchanged.txt"}'}}]}
            return {'role': 'assistant', 'content': 'finished fixture'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        (manager.workspace(session) / 'unchanged.txt').write_text('stable fixture')
        try:
            manager.submit(session, 'Read the file and continue')
            await session.task
            assert '执行进度检查' in windows[3]
            assert 'read_file' in windows[3] and '3' in windows[3]
            assert len(session.record['last_run']['tools']) == 3
            assert all(item['source'] == 'executed' for item in session.record['last_run']['tools'])
            manager.submit(session, 'A separate request')
            await session.task
            assert '执行进度检查' not in windows[4]
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())
