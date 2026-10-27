"""源码统一收纳到 src，开发归档保持私有且位于 docs。"""
from pathlib import Path
import os
import subprocess
import sys
import tomllib

import pytest


def repository_root() -> Path:
    return next(path for path in Path(__file__).resolve().parents if (path / '.env.example').is_file())


def test_workspace_members_are_under_src():
    root = repository_root()
    workspace = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    assert workspace['tool']['uv']['workspace']['members'] == [
        'src/agent_core', 'src/agent_test', 'src/agent_frontend']
    for module in ('AgentCore', 'AgentTest', 'Frontend'):
        assert not (root / module).exists()


@pytest.mark.parametrize('module,package', [
    ('AgentCore', 'agent_core'), ('AgentTest', 'agent_test'), ('Frontend', 'agent_frontend'),
])
def test_modules_keep_packages_directly_under_module_root(module, package):
    source = repository_root() / 'src'
    root = source / package
    assert not (source / module).exists()
    assert not (root / 'src').exists()
    assert not (root / package).exists()
    assert (root / '__init__.py').is_file()
    assert (root / 'pyproject.toml').is_file()


@pytest.mark.parametrize('module,package_file', [
    ('AgentCore', 'agent_core/config.py'),
    ('AgentTest', 'agent_test/scripts/sync_env.py'),
    ('Frontend', 'agent_frontend/main.py'),
])
def test_flat_source_packages_find_workspace_root(tmp_path, monkeypatch, module, package_file):
    from agent_core.project_paths import project_root
    source_root = tmp_path / 'source'
    source_root.mkdir()
    (source_root / '.env.example').touch()
    package = package_file.split('/')[0]
    (source_root / 'pyproject.toml').write_text(
        '[project]\nname = "ai-agent-startup"\n'
        f'[tool.uv.workspace]\nmembers = ["src/{package}"]\n', encoding='utf-8')
    source = source_root / 'src' / package_file
    source.parent.mkdir(parents=True)
    source.touch()
    for directory in source.parents:
        (directory / '__init__.py').touch()
        if directory == source_root / 'src' / package:
            break
    working = tmp_path / 'application'
    working.mkdir()
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    monkeypatch.chdir(working)
    assert project_root(source) == source_root


def test_private_development_archives_are_kept_under_docs():
    root = repository_root()
    assert not (root / 'dev').exists()
    assert '/docs/dev/' in (root / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert 'docs/dev' in (root / '.dockerignore').read_text(encoding='utf-8').splitlines()


def test_source_install_keeps_project_root_outside_working_directory(tmp_path):
    environment = dict(os.environ)
    environment.pop('AI_AGENT_PROJECT_ROOT', None)
    result = subprocess.run([sys.executable, '-c',
        'from agent_core import config; from agent_test.scripts.rag.dataset import ROOT; '
        'assert ROOT == config.PROJECT_ROOT; print(config.PROJECT_ROOT)'],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(repository_root())


def test_installed_core_uses_working_directory_without_source_workspace(tmp_path, monkeypatch):
    from agent_core import config
    installed = tmp_path / 'site-packages/agent_core/config.py'
    installed.parent.mkdir(parents=True)
    installed.touch()
    # 普通应用 pyproject 不能误识别为源码工作区。
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "consumer"\n', encoding='utf-8')
    (tmp_path / '.env.example').touch()
    working = tmp_path / 'application'
    working.mkdir()
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    monkeypatch.setattr(config, '__file__', str(installed))
    monkeypatch.chdir(working)
    assert config._project_root() == working


@pytest.mark.parametrize('project_name', ['consumer', 'ai-agent-startup'])
@pytest.mark.parametrize('package_file', ['agent_core/config.py', 'agent_test/scripts/sync_env.py'])
@pytest.mark.parametrize('installation_root', ['', 'src/agent_core'])
def test_wheel_inside_uv_workspace_uses_current_directory(
        tmp_path, monkeypatch, project_name, package_file, installation_root):
    from agent_core.project_paths import project_root
    consumer = tmp_path / 'consumer'
    consumer.mkdir()
    (consumer / '.env.example').touch()
    (consumer / 'pyproject.toml').write_text(
        f'[project]\nname = "{project_name}"\n'
        '[tool.uv.workspace]\nmembers = ["src/agent_core", "src/agent_test"]\n',
        encoding='utf-8')
    installed = consumer / installation_root / '.venv/lib/python3.12/site-packages' / package_file
    installed.parent.mkdir(parents=True)
    installed.touch()
    working = consumer / 'subproject'
    working.mkdir()
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    monkeypatch.chdir(working)
    assert project_root(installed) == working


def test_engineering_manifest_is_package_resource_outside_working_directory(tmp_path):
    environment = dict(os.environ, AI_AGENT_PROJECT_ROOT=str(tmp_path))
    program = '''
import json
from pathlib import Path
from agent_test.scripts.rag.dataset import DEFAULT_MANIFEST, DEFAULT_CORPUS
assert json.loads(DEFAULT_MANIFEST.read_text(encoding='utf-8'))['document_ids']
assert json.loads(DEFAULT_MANIFEST.with_name('quality.json').read_text(encoding='utf-8'))['document_ids']
assert DEFAULT_CORPUS == Path.cwd() / '.cache/rag/benchmark/engineering/documents.jsonl'
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_release_check_runs_without_installed_dependencies(tmp_path):
    root = repository_root()
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    script = root / 'src/agent_test/scripts/check_release.py'
    result = subprocess.run([sys.executable, '-S', str(script), 'v' + version],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f'Release metadata matches v{version}.'


@pytest.mark.parametrize('module', [
    'sync_env', 'fullstack_validation', 'rag.prepare_assets',
    'rag.prepare_engineering', 'rag.run_engineering', 'rag.quality',
    'replay_compact_trace',
])
def test_engineering_modules_offer_help_outside_source_directory(tmp_path, module):
    result = subprocess.run([sys.executable, '-m', 'agent_test.scripts.' + module, '--help'],
                            cwd=tmp_path, env=dict(os.environ, AI_AGENT_PROJECT_ROOT=str(tmp_path)),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout
