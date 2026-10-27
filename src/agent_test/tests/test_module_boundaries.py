"""三个模块的物理归属和依赖方向不可回退。"""
import ast
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from agent_core import config
from agent_core.core.instrumentation import NullObserver
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_test.runtime import DebugRecorder


def repository_root():
    return next(path for path in Path(__file__).resolve().parents if (path / '.env.example').is_file())


def test_three_installable_modules():
    root = repository_root()
    for package in ('agent_core', 'agent_test', 'agent_frontend'):
        assert (root / 'src' / package / 'pyproject.toml').is_file()
        assert (root / 'src' / package / '__init__.py').is_file()


def test_dependency_direction():
    root = repository_root()
    forbidden = {
        'AgentCore': ('agent_test', 'agent_frontend', 'fastapi', 'uvicorn', 'prompt_toolkit', 'PySide6', 'PyQt6', 'qtpy'),
        'AgentTest': ('agent_frontend',),
    }
    for directory, prefixes in forbidden.items():
        package = {'AgentCore': 'agent_core', 'AgentTest': 'agent_test'}[directory]
        sources = list((root / 'src' / package).rglob('*.py'))
        assert sources, directory
        for source in sources:
            # 工程验收脚本覆盖跨模块集成；运行时诊断适配器仍禁止依赖 Frontend。
            if (directory == 'AgentTest'
                    and source.relative_to(root / 'src' / package).parts[0] in {'scripts', 'tests'}):
                continue
            tree = ast.parse(source.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                for name in names:
                    assert not any(name == prefix or name.startswith(prefix + '.') for prefix in prefixes), (source, name)


def test_package_metadata_preserves_dependency_direction_and_shared_version():
    root = repository_root()
    workspace = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    projects = {
        directory: tomllib.loads((root / 'src' / package / 'pyproject.toml').read_text(encoding='utf-8'))['project']
        for directory, package in [('AgentCore', 'agent_core'), ('AgentTest', 'agent_test'), ('Frontend', 'agent_frontend')]}

    def dependency_names(dependencies):
        return {value.split('[', 1)[0].split('=', 1)[0].split('>', 1)[0].split('<', 1)[0].lower()
                for value in dependencies}

    assert all(project['version'] == workspace['version'] for project in projects.values())
    core_dependencies = dependency_names(projects['AgentCore']['dependencies'])
    assert not {'ai-agent-test', 'ai-agent-frontend', 'fastapi', 'uvicorn', 'prompt-toolkit'} & core_dependencies
    diagnostic_dependencies = dependency_names(projects['AgentTest']['dependencies'])
    assert 'ai-agent-core' in diagnostic_dependencies
    assert 'ai-agent-frontend' not in diagnostic_dependencies
    frontend_dependencies = dependency_names(projects['Frontend']['dependencies'])
    assert 'ai-agent-core' in frontend_dependencies
    assert 'ai-agent-test' not in frontend_dependencies
    assert 'ai-agent-test' in dependency_names(projects['Frontend']['optional-dependencies']['debug'])


def _run_isolated(program, tmp_path, **environment):
    """新进程重新导入模块，避免主测试进程已载入模块掩盖依赖。"""
    env = dict(os.environ, AI_AGENT_PROJECT_ROOT=str(tmp_path), **environment)
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_core_headless_runs_without_diagnostics_frontend_or_ui_dependencies(tmp_path):
    _run_isolated('''
import asyncio, importlib.abc, sys
from pathlib import Path

class BlockUI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'agent_test', 'agent_frontend', 'fastapi', 'uvicorn', 'prompt_toolkit'}:
            raise ModuleNotFoundError('禁止 Core 加载界面或诊断模块: ' + fullname)

sys.meta_path.insert(0, BlockUI())
from agent_core.core.headless import run_headless
from agent_core.core.storage import SessionStore

async def model(history, **kwargs):
    return {'role': 'assistant', 'content': '模块边界测试响应'}

async def run():
    root = Path(sys.argv[1])
    store = SessionStore(root / 'state', root / 'workspace')
    try:
        result = await run_headless(store, '验证独立内核', model=model)
        assert result['exit_code'] == 0
        assert result['messages'][-1]['content'] == '模块边界测试响应'
    finally:
        store.close()

asyncio.run(run())
assert not any(name.startswith(('agent_test', 'agent_frontend')) for name in sys.modules)
''', tmp_path, DEBUG='true', ENABLE_DEBUG_TOOL='true')


def test_core_default_observer_is_empty_even_when_debug_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DEBUG', True)

    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        try:
            assert isinstance(manager.observer, NullObserver)
            assert manager.debug is manager.observer
            assert manager.observer.enabled is False
            assert manager.observer.event('turn.started', session_id='session') is None
            assert not hasattr(manager.observer, 'snapshot')
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())


def test_injected_recorder_receives_real_core_turn_events(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DEBUG', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': '私有测试回答'}

    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        recorder = DebugRecorder(enabled=True)
        manager = SessionManager(store, model=model, observer=recorder)
        try:
            session = manager.create()
            manager.submit(session, '私有测试问题')
            await asyncio.wait_for(session.task, 5)
            assert manager.observer is recorder
            rows = recorder.snapshot()['timeline']
            assert {'turn.started', 'model.completed', 'model.final', 'turn.stopped'} <= {
                row['kind'] for row in rows}
            assert all(row.get('session_id') == session.id for row in rows)
            assert '私有测试问题' not in str(rows)
            assert '私有测试回答' not in str(rows)
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())


def test_frontend_runs_without_optional_diagnostics_package(tmp_path):
    _run_isolated('''
import importlib.abc, sys
from pathlib import Path

class BlockDiagnostics(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'agent_test':
            raise ModuleNotFoundError('诊断扩展未安装')

sys.meta_path.insert(0, BlockDiagnostics())
from fastapi.testclient import TestClient
from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app
root = Path(sys.argv[1])
store = SessionStore(root / 'state', root / 'workspace')
try:
    with TestClient(create_app(store=store, token='boundary-token'),
                    base_url='http://localhost',
                    headers={'Authorization': 'Bearer boundary-token'}) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/meta').status_code == 200
        assert client.get('/debug').status_code == 404
        assert client.get('/api/debug').status_code == 404
        assert client.app.state.manager.observer.enabled is False
finally:
    store.close()
assert 'agent_test' not in sys.modules
''', tmp_path, DEBUG='false', ENABLE_DEBUG_TOOL='false')


def test_project_root_still_resolves_to_workspace_root(monkeypatch):
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    assert config._project_root() == repository_root()


def test_dotenv_and_relative_state_paths_share_explicit_project_root(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.env').write_text('MODULE_BOUNDARY_DOTENV=loaded\n', encoding='utf-8')
    _run_isolated('''
import os, sys
from pathlib import Path
for name in ('AGENT_STATE_DIR', 'SANDBOX_DIR', 'RAG_CACHE_DIR', 'DOC_DIR', 'AUDIT_LOG'):
    os.environ.pop(name, None)
from agent_core import config
root = Path(sys.argv[1])
assert config.PROJECT_ROOT == root
assert os.environ['MODULE_BOUNDARY_DOTENV'] == 'loaded'
assert config.AGENT_STATE_DIR == root / '.agent'
assert config.SANDBOX_DIR == root / 'crud_tests'
assert config.RAG_CACHE_DIR == root / '.cache/rag'
assert config.DOC_DIR == root / 'data/raw'
assert config.AUDIT_LOG == root / 'logs/audit.log'
''', project, PYTHON_DOTENV_DISABLED='0')
