import importlib
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from agent_core.tools import wsl_command
from agent_core.tools.sandbox import SandboxError
from agent_core.core.workspaces import SessionWorkspaces


@pytest.fixture(autouse=True)
def isolated_portable_resources_and_environment(tmp_path, monkeypatch):
    """冻结入口使用受控公开资源，不能读取实际用户目录或保留缓存变量。"""
    resources = tmp_path / 'bundle' / 'portable_resources'
    resources.mkdir(parents=True)
    for filename in ('configuration.example.env', 'LICENSE.txt', '使用说明.txt'):
        (resources / filename).write_text('# controlled public fixture\n', encoding='utf-8')
    (resources / 'WSL').mkdir()
    (resources / 'WSL/requirements.txt').write_text('# controlled lock\n', encoding='utf-8')
    (resources / 'THIRD_PARTY_LICENSES').mkdir()
    monkeypatch.setattr(sys, '_MEIPASS', str(resources.parent), raising=False)
    for name in ('AI_AGENT_PROJECT_ROOT', 'AI_AGENT_PORTABLE', 'HF_HOME', 'TORCH_HOME',
                 'XDG_CACHE_HOME', 'TEMP', 'TMP', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE',
                 'TRANSFORMERS_CACHE', 'HF_MODULES_CACHE', 'SENTENCE_TRANSFORMERS_HOME',
                 'FASTEMBED_CACHE_PATH'):
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, 'tempdir', tempfile.tempdir)


def test_frozen_worker_fails_closed_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    with pytest.raises(SandboxError):
        wsl_command._worker_script()


def test_frozen_launcher_uses_executable_directory_independent_of_cwd(monkeypatch, tmp_path):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    # 在任意回归宿主上显式验证 Windows 分支，Linux 自有 XDG 用例。
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    install = tmp_path / 'portable-install'
    install.mkdir()
    monkeypatch.setattr(sys, 'executable', str(install / 'ChannelAgent.exe'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['app'])
    monkeypatch.setattr(launcher.multiprocessing, 'freeze_support', lambda: None)
    app = ModuleType('agent_frontend.desktop.app')
    app.main = lambda: None
    monkeypatch.setitem(sys.modules, 'agent_frontend.desktop.app', app)
    launcher.main()
    assert Path(launcher.os.environ['AI_AGENT_PROJECT_ROOT']) == install
    assert Path.cwd() == tmp_path


def test_frozen_launcher_replaces_empty_root(monkeypatch, tmp_path):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    install = tmp_path / 'portable-install'
    install.mkdir()
    monkeypatch.setattr(sys, 'executable', str(install / 'ChannelAgent.exe'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setenv('AI_AGENT_PROJECT_ROOT', '')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['app'])
    monkeypatch.setattr(launcher.multiprocessing, 'freeze_support', lambda: None)
    app = ModuleType('agent_frontend.desktop.app')
    app.main = lambda: None
    monkeypatch.setitem(sys.modules, 'agent_frontend.desktop.app', app)
    launcher.main()
    assert Path(launcher.os.environ['AI_AGENT_PROJECT_ROOT']) == install


def test_worker_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    assert wsl_command._worker_script() == Path(wsl_command.__file__).with_name('wsl_command_worker.py')
    worker = tmp_path / 'wsl_runtime' / 'agent_core' / 'tools' / 'wsl_command_worker.py'
    worker.parent.mkdir(parents=True)
    worker.touch()
    monkeypatch.setattr(sys, 'frozen', True)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    assert wsl_command._worker_script() == worker


@pytest.mark.parametrize('frozen', [True, False])
def test_launcher_preserves_override_and_repairs_streams(monkeypatch, tmp_path, frozen):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    monkeypatch.setenv('AI_AGENT_PROJECT_ROOT', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['app'])
    monkeypatch.setattr(launcher.multiprocessing, 'freeze_support', lambda: None)
    for name in ('stdin', 'stdout', 'stderr'):
        monkeypatch.setattr(sys, name, None)
    app = ModuleType('agent_frontend.desktop.app')
    app.main = lambda: None
    monkeypatch.setitem(sys.modules, 'agent_frontend.desktop.app', app)
    launcher.main()
    assert launcher.os.environ['AI_AGENT_PROJECT_ROOT'] == str(tmp_path)
    for name in ('stdin', 'stdout', 'stderr'):
        stream = getattr(sys, name)
        assert stream is not None
        stream.close()


def test_self_check_returns_exit_code(monkeypatch, tmp_path):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    monkeypatch.setattr(launcher.multiprocessing, 'freeze_support', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['app', '--self-check', str(tmp_path / 'report')])
    smoke = ModuleType('agent_test.scripts.packaged_smoke')
    smoke.run = lambda path: 7 if path == tmp_path / 'report' else 9
    monkeypatch.setitem(sys.modules, 'agent_test.scripts.packaged_smoke', smoke)
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 7


def test_source_launcher_does_not_set_root(monkeypatch):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    monkeypatch.setattr(sys, 'argv', ['app'])
    monkeypatch.setattr(launcher.multiprocessing, 'freeze_support', lambda: None)
    app = ModuleType('agent_frontend.desktop.app')
    app.main = lambda: None
    monkeypatch.setitem(sys.modules, 'agent_frontend.desktop.app', app)
    launcher.main()
    assert 'AI_AGENT_PROJECT_ROOT' not in launcher.os.environ


def test_launch_error_dialog_contains_only_exception_class(monkeypatch):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    monkeypatch.setattr(launcher.multiprocessing, 'freeze_support', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['app'])
    app_module = ModuleType('agent_frontend.desktop.app')
    def fail():
        raise ValueError('secret text')
    app_module.main = fail
    monkeypatch.setitem(sys.modules, 'agent_frontend.desktop.app', app_module)
    widgets = importlib.import_module('PySide6.QtWidgets')
    messages = []
    monkeypatch.setattr(widgets.QApplication, 'instance', lambda: object())
    monkeypatch.setattr(widgets.QMessageBox, 'critical', lambda *args: messages.append(args))
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 1
    assert messages[0][2] == '启动失败：ValueError'


def test_frozen_installation_is_protected_from_workspace_selection(monkeypatch, tmp_path):
    install = tmp_path / 'install'
    runtime = install / '_internal'
    child = runtime / 'child'
    unrelated = tmp_path / 'project'
    child.mkdir(parents=True)
    unrelated.mkdir()
    executable = install / 'ChannelAgent.exe'
    executable.touch()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, '_MEIPASS', str(runtime), raising=False)
    manager = SimpleNamespace(sessions={}, store=SimpleNamespace(root=tmp_path / 'state', workspace_root=tmp_path / 'workspaces'))
    workspaces = SessionWorkspaces(manager)
    for path in (install, child, tmp_path):
        with pytest.raises(PermissionError):
            workspaces.validate_path(str(path))
    assert workspaces.validate_path(str(unrelated))[0] == str(unrelated)
