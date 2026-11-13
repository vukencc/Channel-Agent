"""便携根的配置与工作区边界；全部使用独立临时目录。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_portable_config_defaults_follow_selected_root(tmp_path):
    env = {**os.environ, 'AI_AGENT_PROJECT_ROOT': str(tmp_path),
           'AI_AGENT_PORTABLE': '1', 'PYTHON_DOTENV_DISABLED': '1'}
    names = ('AGENT_STATE_DIR', 'SANDBOX_DIR', 'RAG_CACHE_DIR', 'RUNTIME_MODEL_DIR',
             'DOC_DIR', 'AUDIT_LOG')
    for name in names:
        env.pop(name, None)
    code = ('import json; from agent_core import config; '
            'print(json.dumps({name:str(getattr(config,name,None)) for name in '
            + repr(names) + '}))')
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10, check=True)
    actual = json.loads(result.stdout)
    for name, relative in {'AGENT_STATE_DIR': 'data/state', 'SANDBOX_DIR': 'data/workspaces',
                           'RAG_CACHE_DIR': 'cache/rag', 'RUNTIME_MODEL_DIR': 'config',
                           'DOC_DIR': 'data/raw', 'AUDIT_LOG': 'logs/audit.log'}.items():
        assert Path(actual[name]) == tmp_path / relative


@pytest.fixture
def portable_core(tmp_path, monkeypatch):
    root = tmp_path / 'portable'
    root.mkdir()
    (root / 'ChannelAgent.exe').write_bytes(b'fixture-executable')
    runtime = tmp_path / 'extracted-runtime'
    runtime.mkdir()
    for name, value in {'PROJECT_ROOT': root, 'PORTABLE_MODE': True,
                        'AGENT_STATE_DIR': root / 'data/state',
                        'SANDBOX_DIR': root / 'data/workspaces',
                        'DOC_DIR': root / 'data/raw', 'RAG_CACHE_DIR': root / 'cache/rag',
                        'RUNTIME_MODEL_DIR': root / 'config',
                        'AUDIT_LOG': root / 'logs/audit.log', 'TOOL_ROOTS': {}, 'RAG_SOURCES': {}}.items():
        monkeypatch.setattr(config, name, value, raising=False)
    for relative in ('data/state', 'data/workspaces', 'data/projects/default', 'data/raw',
                     'cache/rag', 'logs', 'config'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / '.env').write_text('# fixture\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'executable', str(root / 'ChannelAgent.exe'))
    monkeypatch.setattr(sys, '_MEIPASS', str(runtime), raising=False)
    store = SessionStore(config.AGENT_STATE_DIR, config.SANDBOX_DIR)
    manager = SessionManager(store)
    yield root, runtime, store, manager
    store.close()


def test_portable_default_registry_is_private_config(portable_core):
    root, _, _, manager = portable_core
    assert manager.model_registry.root == root / 'config'


def test_explicit_independent_state_does_not_share_portable_registry(portable_core, tmp_path):
    _, _, _, _ = portable_core
    store = SessionStore(tmp_path / 'separate-state', tmp_path / 'separate-workspaces')
    try:
        assert SessionManager(store).model_registry.root == store.root
    finally:
        store.close()


def test_portable_project_can_be_selected_without_exposing_private_roots(portable_core):
    root, runtime, _, manager = portable_core
    project = root / 'data/projects/default'
    assert manager.workspaces.validate_path(str(project))[0] == str(project)
    for path in (root, root.parent, root / 'config', root / 'data/state',
                 root / 'data/workspaces', root / 'data/raw', root / 'cache',
                 root / 'logs', root / 'data', runtime):
        with pytest.raises(PermissionError):
            manager.workspaces.validate_path(str(path))


def test_source_repository_stays_protected_when_portable_flag_is_set(portable_core, monkeypatch):
    root, _, _, manager = portable_core
    monkeypatch.setattr(sys, 'frozen', False)
    with pytest.raises(PermissionError):
        manager.workspaces.validate_path(str(root / 'data/projects/default'))


def test_configured_supervisor_credentials_remain_protected(portable_core, monkeypatch):
    root, _, _, manager = portable_core
    project = root / 'data/projects/default'
    secret = project / 'supervisor.env'
    secret.write_text('SUPERVISOR_API_KEY=offline-fixture\n', encoding='utf-8')
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', secret)
    with pytest.raises(PermissionError):
        manager.workspaces.validate_path(str(project))
