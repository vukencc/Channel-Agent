"""便携初始化离线回归；frozen 路径由受控夹具模拟，并非实际 exe 验收。"""
import importlib
import errno
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import ModuleType
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def restore_bootstrap_environment(monkeypatch):
    """初始化会主动设置缓存变量，回归结束后恢复宿主原值。"""
    for name in ('AI_AGENT_PROJECT_ROOT', 'AI_AGENT_PORTABLE', 'HF_HOME', 'TORCH_HOME',
                 'XDG_CACHE_HOME', 'TEMP', 'TMP', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE',
                 'TRANSFORMERS_CACHE', 'HF_MODULES_CACHE', 'SENTENCE_TRANSFORMERS_HOME',
                 'FASTEMBED_CACHE_PATH'):
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, 'tempdir', tempfile.tempdir)


def private_file(path):
    if os.name == 'nt':
        from agent_core.core.windows_security import protect_private_path
        protect_private_path(path)
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


@pytest.fixture
def portable():
    return importlib.import_module('agent_frontend.desktop.portable')


@pytest.fixture
def frozen_launcher(tmp_path, monkeypatch):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    install = tmp_path / '移动 中文 path' / '应用'
    install.mkdir(parents=True)
    executable = install / 'ChannelAgent.exe'
    executable.touch()
    extracted = tmp_path / '_MEI private'
    extracted.mkdir()
    resources = extracted / 'portable_resources'
    resources.mkdir()
    (resources / 'configuration.example.env').write_bytes(b'# public template\n')
    (resources / 'LICENSE.txt').write_bytes(b'controlled public license')
    (resources / '使用说明.txt').write_text('受控公开说明', encoding='utf-8')
    (resources / 'WSL').mkdir()
    (resources / 'WSL/requirements.txt').write_bytes(b'# controlled lock\n')
    (resources / 'THIRD_PARTY_LICENSES').mkdir()
    widgets = ModuleType('PySide6.QtWidgets')
    widgets.QApplication = SimpleNamespace(instance=lambda: object())
    widgets.QMessageBox = SimpleNamespace(critical=lambda *args: None)
    monkeypatch.setitem(sys.modules, 'PySide6.QtWidgets', widgets)
    working = tmp_path / 'unrelated cwd'
    working.mkdir()
    local = tmp_path / 'user-local'
    old = local / 'ChannelAgent'
    old.mkdir(parents=True)
    (old / '.env').write_bytes(b'PRIVATE_EXISTING=unchanged\n')
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, '_MEIPASS', str(extracted), raising=False)
    monkeypatch.setattr(sys, 'argv', [str(executable)])
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    monkeypatch.delenv('AI_AGENT_PORTABLE', raising=False)
    monkeypatch.chdir(working)
    monkeypatch.setattr(launcher.multiprocessing, 'freeze_support', lambda: None)
    calls = []
    app = ModuleType('agent_frontend.desktop.app')
    app.main = lambda: calls.append(list(sys.argv))
    monkeypatch.setitem(sys.modules, 'agent_frontend.desktop.app', app)
    return launcher, install, extracted, working, local, calls


def test_first_start_creates_complete_layout_without_source_template(portable, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / '独立 portable'
    layout = portable.initialize_layout(root)
    assert layout.root == root
    expected = {
        'config': 'config', 'state': 'data/state', 'workspaces': 'data/workspaces',
        'projects': 'data/projects/default', 'raw': 'data/raw', 'rag_cache': 'cache/rag',
    }
    for attribute, relative in expected.items():
        assert getattr(layout, attribute) == root / relative
        assert (root / relative).is_dir()
    for relative in ('cache/huggingface', 'cache/torch', 'cache/tmp', 'logs'):
        assert (root / relative).is_dir()
    assert layout.env_file == root / '.env'
    assert layout.env_file.is_file()
    assert '.env.example' not in {path.name for path in tmp_path.iterdir()}


def test_initialization_preserves_existing_user_bytes(portable, tmp_path):
    root = tmp_path / 'portable'
    root.mkdir()
    env = root / '.env'
    env.write_bytes(b'PRIVATE_TOKEN=sentinel\nTOOL_PERMISSION_POLICY=readonly\n')
    private_file(env)
    config = root / 'config'
    config.mkdir()
    private_file(config)
    model = config / 'models.json'
    model.write_bytes(b'{"user": "preserve"}\n')
    portable.initialize_layout(root)
    portable.initialize_layout(root)
    assert env.read_bytes() == b'PRIVATE_TOKEN=sentinel\nTOOL_PERMISSION_POLICY=readonly\n'
    assert model.read_bytes() == b'{"user": "preserve"}\n'


@pytest.mark.parametrize('relative', ['.env', 'config', 'data', 'data/state', 'data/projects', 'cache', 'cache/rag', 'logs'])
def test_symlink_layout_is_rejected_without_touching_target(portable, tmp_path, relative, symlink_capable):
    root = tmp_path / 'portable'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'untouched')
    if relative == '.env':
        target = sentinel
    else:
        target = outside
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    (root / relative).symlink_to(target, target_is_directory=relative != '.env')
    with pytest.raises(portable.PortableError) as error:
        portable.initialize_layout(root)
    assert error.value.kind == 'unsafe'
    assert sentinel.read_bytes() == b'untouched'
    assert sorted(path.name for path in outside.iterdir()) == ['sentinel']


def test_hardlinked_environment_is_rejected(portable, tmp_path):
    root = tmp_path / 'portable'
    root.mkdir()
    original = tmp_path / 'private.env'
    original.write_bytes(b'PRIVATE=preserve\n')
    os.link(original, root / '.env')
    with pytest.raises(portable.PortableError) as error:
        portable.initialize_layout(root)
    assert error.value.kind == 'unsafe'
    assert original.read_bytes() == b'PRIVATE=preserve\n'


def test_environment_directory_is_rejected(portable, tmp_path):
    root = tmp_path / 'portable'
    (root / '.env').mkdir(parents=True)
    with pytest.raises(portable.PortableError):
        portable.initialize_layout(root)


def test_default_uses_executable_not_cwd_meipass_or_old_state(frozen_launcher):
    launcher, install, extracted, working, local, calls = frozen_launcher
    launcher.main()
    assert Path(os.environ['AI_AGENT_PROJECT_ROOT']) == install
    assert os.environ['AI_AGENT_PORTABLE'] == '1'
    assert (install / '.env').is_file()
    assert sorted(path.name for path in extracted.iterdir()) == ['portable_resources']
    assert (extracted / 'portable_resources/LICENSE.txt').read_bytes() == b'controlled public license'
    assert not list(working.iterdir())
    assert (local / 'ChannelAgent/.env').read_bytes() == b'PRIVATE_EXISTING=unchanged\n'
    assert sorted(path.name for path in local.iterdir()) == ['ChannelAgent']
    assert len(calls) == 1


@pytest.mark.parametrize('mode', ['environment', 'argument', 'user'])
def test_explicit_root_precedence(frozen_launcher, monkeypatch, tmp_path, mode):
    launcher, install, extracted, working, local, calls = frozen_launcher
    environment = tmp_path / 'environment'
    argument = tmp_path / 'selected path'
    monkeypatch.setenv('AI_AGENT_PROJECT_ROOT', str(environment))
    arguments = []
    expected = environment
    if mode == 'argument':
        arguments = ['--data-dir', str(argument)]
        expected = argument
    elif mode == 'user':
        arguments = ['--use-user-data']
        expected = local / 'ChannelAgentPortable'
    monkeypatch.setattr(sys, 'argv', [sys.executable, *arguments, '--state-dir', str(tmp_path / 'session-only')])
    launcher.main()
    assert Path(os.environ['AI_AGENT_PROJECT_ROOT']) == expected
    assert (expected / '.env').is_file()
    assert calls == [[sys.executable, '--state-dir', str(tmp_path / 'session-only')]]
    assert not (install / '.env').exists()


def test_portable_info_initializes_without_gui_and_redacts_config(frozen_launcher, monkeypatch, tmp_path):
    launcher, install, extracted, working, local, calls = frozen_launcher
    (install / '.env').write_text('PRIVATE_KEY=never-output-this\n', encoding='utf-8')
    private_file(install / '.env')
    report = tmp_path / 'portable-info.json'
    monkeypatch.setattr(sys, 'argv', [sys.executable, '--portable-info', str(report)])
    with pytest.raises(SystemExit) as result:
        launcher.main()
    assert result.value.code == 0
    payload = json.loads(report.read_text(encoding='utf-8'))
    assert Path(payload['root']) == install
    assert {'config', 'state', 'workspaces', 'projects', 'raw', 'rag_cache', 'env_file'} <= payload['paths'].keys()
    assert 'never-output-this' not in report.read_text(encoding='utf-8')
    assert calls == []


def test_launcher_import_does_not_load_config(tmp_path):
    script = 'import sys; import agent_frontend.desktop.launcher; assert "agent_core.config" not in sys.modules'
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('failure', [OSError(errno.EIO, 'controlled write failure'), KeyboardInterrupt()])
def test_failed_atomic_environment_publish_leaves_no_partial_file(portable, tmp_path, monkeypatch, failure):
    root = tmp_path / 'portable'
    original = portable.os.fsync
    def fail(fd):
        raise failure
    monkeypatch.setattr(portable.os, 'fsync', fail)
    with pytest.raises(portable.PortableError if isinstance(failure, OSError) else KeyboardInterrupt):
        portable.initialize_layout(root)
    assert not (root / '.env').exists()
    assert not list(root.glob('.portable-env-*'))
    monkeypatch.setattr(portable.os, 'fsync', original)
    portable.initialize_layout(root)
    assert (root / '.env').is_file()


def test_atomic_environment_publish_does_not_replace_concurrent_user_file(portable, tmp_path, monkeypatch):
    if os.name == 'nt':
        from agent_core.core import windows_files
        original = windows_files.rename_noreplace
        def racing_publish(source, target, **kwargs):
            target.write_bytes(b'USER_WINNER=preserved\n')
            private_file(target)
            return original(source, target, **kwargs)
        monkeypatch.setattr(windows_files, 'rename_noreplace', racing_publish)
    else:
        original = os.link
        def racing_publish(source, target, **kwargs):
            target.write_bytes(b'USER_WINNER=preserved\n')
            private_file(target)
            return original(source, target, **kwargs)
        monkeypatch.setattr(portable.os, 'link', racing_publish)
    root = tmp_path / 'portable'
    portable.initialize_layout(root)
    assert (root / '.env').read_bytes() == b'USER_WINNER=preserved\n'
    assert not list(root.glob('.portable-env-*'))


def test_first_start_is_serialized_across_real_processes(portable, tmp_path):
    root = tmp_path / '并发 first start'
    script = ('import sys; from pathlib import Path; '
              'from agent_frontend.desktop.portable import initialize_layout; '
              'initialize_layout(Path(sys.argv[1]))')
    processes = [subprocess.Popen([sys.executable, '-c', script, str(root)], cwd=tmp_path,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(4)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    content = (root / '.env').read_bytes()
    portable.initialize_layout(root)
    assert (root / '.env').read_bytes() == content
    assert not list(root.glob('.portable-env-*'))


@pytest.mark.skipif(os.name == 'nt', reason='Linux flock 的受控超时注入；Windows 锁由 Native 专项验收')
def test_initialization_lock_timeout_is_bounded_and_does_not_publish(portable, tmp_path, monkeypatch):
    import fcntl
    ticks = iter([0, 11])
    monkeypatch.setattr(portable.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(portable.time, 'sleep', lambda seconds: pytest.fail('过期后不能继续等待'))
    def busy(fd, operation):
        raise BlockingIOError(errno.EAGAIN, 'controlled held lock')
    monkeypatch.setattr(fcntl, 'flock', busy)
    root = tmp_path / 'portable'
    with pytest.raises(portable.PortableError):
        portable.initialize_layout(root)
    assert not (root / '.env').exists()


@pytest.mark.parametrize('mode', ['argument', 'environment', 'user', 'info', 'self-check'])
def test_explicit_or_noninteractive_failure_never_opens_fallback(frozen_launcher, portable, monkeypatch, tmp_path, mode):
    launcher, install, extracted, working, local, calls = frozen_launcher
    selected = tmp_path / 'denied-root'
    arguments = []
    if mode == 'argument':
        arguments = ['--data-dir', str(selected)]
    elif mode == 'environment':
        monkeypatch.setenv('AI_AGENT_PROJECT_ROOT', str(selected))
    elif mode == 'user':
        selected = local / 'ChannelAgentPortable'
        arguments = ['--use-user-data']
    elif mode == 'self-check':
        selected = install
        arguments = ['--self-check', str(tmp_path / 'self-check.json')]
    else:
        selected = install
        arguments = ['--portable-info', str(tmp_path / 'error.json')]
    monkeypatch.setattr(sys, 'argv', [sys.executable, *arguments])
    def denied(root):
        raise portable.PortableError(root, '创建目录', 'unwritable')
    monkeypatch.setattr(portable, 'initialize_layout', denied)
    monkeypatch.setattr(launcher, 'choose_portable_root', lambda error: pytest.fail('显式目录不能静默回退'))
    # 错误提示为受控 UI fixture，不能启动真实对话框。
    widgets = importlib.import_module('PySide6.QtWidgets')
    monkeypatch.setattr(widgets.QApplication, 'instance', lambda: object())
    monkeypatch.setattr(widgets.QMessageBox, 'critical', lambda *args: None)
    with pytest.raises(SystemExit) as result:
        launcher.main()
    assert result.value.code != 0
    assert calls == []
    if mode == 'info':
        payload = json.loads((tmp_path / 'error.json').read_text(encoding='utf-8'))
        assert payload['error']['type'] == 'unwritable'
        assert Path(payload['error']['path']) == selected


def test_moved_portable_directory_preserves_data_and_relocates_cache(frozen_launcher, monkeypatch, tmp_path):
    launcher, install, extracted, working, local, calls = frozen_launcher
    launcher.main()
    user = install / 'data/state/user-sentinel.json'
    user.write_bytes(b'{"preserve": true}\n')
    environment = (install / '.env').read_bytes()
    moved = tmp_path / '另一 位置' / 'portable moved'
    moved.parent.mkdir()
    install.rename(moved)
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    monkeypatch.setattr(sys, 'executable', str(moved / 'ChannelAgent.exe'))
    monkeypatch.setattr(sys, 'argv', [sys.executable])
    launcher.main()
    assert Path(os.environ['AI_AGENT_PROJECT_ROOT']) == moved
    assert Path(os.environ['HF_HOME']) == moved / 'cache/huggingface'
    assert Path(os.environ['TORCH_HOME']) == moved / 'cache/torch'
    assert Path(os.environ['TEMP']) == moved / 'cache/tmp'
    assert (moved / '.env').read_bytes() == environment
    assert (moved / 'data/state/user-sentinel.json').read_bytes() == b'{"preserve": true}\n'
    assert not install.exists()
    assert not list(working.iterdir())
    assert sorted(path.name for path in local.iterdir()) == ['ChannelAgent']


@pytest.mark.parametrize('selection', ['cancel', 'user', 'custom'])
def test_default_unwritable_root_respects_user_selection(frozen_launcher, portable, monkeypatch, tmp_path, selection):
    launcher, install, extracted, working, local, calls = frozen_launcher
    original = portable.initialize_layout
    seen = []
    def fail_install(root):
        if Path(root) == install:
            raise portable.PortableError(root, '写入目录', 'unwritable')
        return original(root)
    expected = None if selection == 'cancel' else local / 'ChannelAgentPortable' if selection == 'user' else tmp_path / '手选 root'
    def choose(error):
        seen.append((error.path, error.operation, error.kind))
        return expected
    monkeypatch.setattr(portable, 'initialize_layout', fail_install)
    monkeypatch.setattr(launcher, 'choose_portable_root', choose)
    if selection == 'cancel':
        with pytest.raises(SystemExit) as result:
            launcher.main()
        assert result.value.code != 0
        assert calls == []
        assert not (local / 'ChannelAgentPortable').exists()
    else:
        launcher.main()
        assert Path(os.environ['AI_AGENT_PROJECT_ROOT']) == expected
        assert (expected / '.env').is_file()
        assert len(calls) == 1
    assert seen == [(install, '写入目录', 'unwritable')]


@pytest.mark.parametrize('kind', ['unsafe', 'invalid', 'io'])
def test_unsafe_default_never_offers_fallback(frozen_launcher, portable, monkeypatch, kind):
    launcher, install, extracted, working, local, calls = frozen_launcher
    def fail(root):
        raise portable.PortableError(root, '检查目录', kind)
    monkeypatch.setattr(portable, 'initialize_layout', fail)
    monkeypatch.setattr(launcher, 'choose_portable_root', lambda error: pytest.fail('不安全目录不能进入fallback'))
    widgets = importlib.import_module('PySide6.QtWidgets')
    monkeypatch.setattr(widgets.QApplication, 'instance', lambda: object())
    monkeypatch.setattr(widgets.QMessageBox, 'critical', lambda *args: None)
    with pytest.raises(SystemExit):
        launcher.main()
    assert calls == []


def test_missing_bundled_resources_fails_noninteractive(frozen_launcher, monkeypatch, tmp_path):
    launcher, install, extracted, working, local, calls = frozen_launcher
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'missing-bundle'))
    report = tmp_path / 'failure.json'
    monkeypatch.setattr(sys, 'argv', [sys.executable, '--portable-info', str(report)])
    monkeypatch.setattr(launcher, 'choose_portable_root', lambda error: pytest.fail('缺失运行资源不得fallback'))
    with pytest.raises(SystemExit) as result:
        launcher.main()
    assert result.value.code != 0
    assert json.loads(report.read_text(encoding='utf-8'))['error']['type'] == 'invalid'
    assert calls == []


@pytest.mark.parametrize('content', [b'=missing-key\n', b'KEY="unterminated\n', b'KEY=\x00\n', b'KEY=\xff\n'])
def test_invalid_environment_is_rejected_without_repair(portable, tmp_path, content):
    root = tmp_path / 'portable'
    root.mkdir()
    env = root / '.env'
    env.write_bytes(content)
    private_file(env)
    with pytest.raises(portable.PortableError) as error:
        portable.initialize_layout(root)
    assert error.value.kind == 'invalid'
    assert env.read_bytes() == content


def test_dotenv_bare_key_is_preserved_without_loading_environment(portable, tmp_path, monkeypatch):
    root = tmp_path / 'portable'
    root.mkdir()
    content = b'PORTABLE_BARE_KEY\nNORMAL_VALUE=preserved\n'
    env = root / '.env'
    env.write_bytes(content)
    private_file(env)
    monkeypatch.delenv('PORTABLE_BARE_KEY', raising=False)
    monkeypatch.delenv('NORMAL_VALUE', raising=False)
    portable.initialize_layout(root)
    assert env.read_bytes() == content
    assert 'PORTABLE_BARE_KEY' not in os.environ
    assert 'NORMAL_VALUE' not in os.environ


def test_write_denial_is_classified_without_leaving_partial_env(portable, tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, 'controlled access denied')
    monkeypatch.setattr(portable.tempfile, 'mkstemp', denied)
    root = tmp_path / 'portable'
    with pytest.raises(portable.PortableError) as error:
        portable.initialize_layout(root)
    assert error.value.kind == 'unwritable'
    assert not (root / '.env').exists()
    assert not list(root.glob('.portable-env-*'))


def test_new_managed_directories_are_private_without_changing_existing_parent(portable, tmp_path):
    root = tmp_path / 'portable'
    root.mkdir()
    old_mode = root.stat().st_mode
    portable.initialize_layout(root)
    for relative in ('config', 'data', 'data/state', 'data/workspaces', 'data/projects',
                     'data/projects/default', 'data/raw', 'cache', 'cache/rag',
                     'cache/huggingface', 'cache/huggingface/hub', 'cache/huggingface/modules',
                     'cache/sentence-transformers', 'cache/fastembed', 'cache/torch', 'cache/tmp', 'logs'):
        path = root / relative
        if os.name == 'nt':
            from agent_core.core.windows_security import verify_private_path
            verify_private_path(path)
        else:
            assert path.stat().st_mode & 0o077 == 0, relative
    assert root.stat().st_mode == old_mode


@pytest.mark.parametrize('policy', ['readonly', 'standard', 'smart'])
def test_configure_environment_preserves_selected_tool_permission(portable, tmp_path, monkeypatch, sandbox_env, policy):
    from agent_core import config
    monkeypatch.setenv('TOOL_PERMISSION_POLICY', policy)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    layout = portable.initialize_layout(tmp_path / 'portable')
    portable.configure_environment(layout)
    assert os.environ['TOOL_PERMISSION_POLICY'] == policy
    assert config.TOOL_PERMISSION_POLICY == policy
    assert config.SANDBOX_DIR == sandbox_env


def test_configure_environment_relocates_all_caches_and_resets_early_tempfile_cache(portable, tmp_path, monkeypatch):
    root = tmp_path / '便携 cache root'
    layout = portable.initialize_layout(root)
    early_temp = tmp_path / 'previous-system-temp'
    early_temp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(early_temp))
    assert Path(tempfile.gettempdir()) == early_temp
    expected = {
        'HF_HOME': 'cache/huggingface', 'TORCH_HOME': 'cache/torch',
        'XDG_CACHE_HOME': 'cache', 'TEMP': 'cache/tmp', 'TMP': 'cache/tmp',
        'HF_HUB_CACHE': 'cache/huggingface/hub', 'HUGGINGFACE_HUB_CACHE': 'cache/huggingface/hub',
        'TRANSFORMERS_CACHE': 'cache/huggingface/hub', 'HF_MODULES_CACHE': 'cache/huggingface/modules',
        'SENTENCE_TRANSFORMERS_HOME': 'cache/sentence-transformers', 'FASTEMBED_CACHE_PATH': 'cache/fastembed',
    }
    for name in expected:
        monkeypatch.setenv(name, str(tmp_path / 'old-development-cache' / name))
    portable.configure_environment(layout)
    for name, relative in expected.items():
        assert Path(os.environ[name]) == root / relative
        assert (root / relative).is_dir()
    assert Path(tempfile.gettempdir()) == root / 'cache/tmp'
    with tempfile.TemporaryFile() as stream:
        stream.write(b'controlled temporary payload')
    with tempfile.NamedTemporaryFile() as stream:
        assert Path(stream.name).parent == root / 'cache/tmp'
    assert not list(early_temp.iterdir())
    assert not (tmp_path / 'old-development-cache').exists()


@pytest.mark.parametrize('kind', ['env', 'file', 'symlink', 'hardlink', 'directory'])
def test_info_report_cannot_overwrite_existing_user_path(frozen_launcher, monkeypatch, tmp_path, kind, symlink_capable):
    launcher, install, extracted, working, local, calls = frozen_launcher
    sentinel = tmp_path / 'user-sentinel'
    sentinel.write_bytes(b'USER_CONFIG=preserve-exactly\n')
    private_file(sentinel)
    report = tmp_path / 'existing-report.json'
    if kind == 'env':
        report = install / '.env'
        report.write_bytes(sentinel.read_bytes())
        private_file(report)
    elif kind == 'file':
        report.write_bytes(sentinel.read_bytes())
    elif kind == 'symlink':
        report.symlink_to(sentinel)
    elif kind == 'hardlink':
        os.link(sentinel, report)
    else:
        report.mkdir()
    monkeypatch.setattr(sys, 'argv', [sys.executable, '--portable-info', str(report)])
    with pytest.raises(SystemExit) as result:
        launcher.main()
    assert result.value.code != 0
    assert sentinel.read_bytes() == b'USER_CONFIG=preserve-exactly\n'
    if kind != 'directory':
        assert report.read_bytes() == b'USER_CONFIG=preserve-exactly\n'
    else:
        assert not list(report.iterdir())
    assert calls == []


@pytest.mark.parametrize('failure', [OSError(errno.EIO, 'controlled report sync failure'), KeyboardInterrupt()])
def test_report_atomic_failure_never_publishes_partial_json(tmp_path, monkeypatch, portable, failure):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    report = tmp_path / 'unique-info.json'
    def fail(fd):
        raise failure
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises((OSError, portable.PortableError) if isinstance(failure, OSError) else KeyboardInterrupt):
        launcher._write_info(report, {'root': 'controlled', 'paths': {}})
    assert not report.exists()
    assert not list(tmp_path.iterdir())


def test_report_partial_serialization_failure_cleans_temporary_file(tmp_path, monkeypatch):
    launcher = importlib.import_module('agent_frontend.desktop.launcher')
    report = tmp_path / 'unique-report.json'
    def partial(payload, stream, **kwargs):
        stream.write('{"root":')
        raise OSError(errno.EIO, 'controlled partial serialization failure')
    monkeypatch.setattr(launcher.json, 'dump', partial)
    with pytest.raises(OSError):
        launcher._write_info(report, {'root': 'controlled'})
    assert not report.exists()
    assert not list(tmp_path.iterdir())


def test_script_entry_bootstraps_without_package_context(frozen_launcher, monkeypatch, tmp_path):
    """runpy 仅验证入口脚本语义；不声称执行真实 PyInstaller exe。"""
    launcher, install, extracted, working, local, calls = frozen_launcher
    report = tmp_path / 'script-entry.json'
    monkeypatch.setattr(sys, 'argv', [sys.executable, '--portable-info', str(report)])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(launcher.__file__, run_name='__main__')
    assert result.value.code == 0
    assert Path(json.loads(report.read_text(encoding='utf-8'))['root']) == install
    assert calls == []


@pytest.mark.parametrize('kind', ['env', 'file', 'symlink', 'hardlink', 'directory'])
def test_self_check_cannot_overwrite_existing_user_path(frozen_launcher, monkeypatch, tmp_path, kind, symlink_capable):
    launcher, install, extracted, working, local, calls = frozen_launcher
    sentinel = tmp_path / 'private-sentinel'
    sentinel.write_bytes(b'PRIVATE_USER=preserve\n')
    private_file(sentinel)
    report = tmp_path / 'self-check.json'
    if kind == 'env':
        report = install / '.env'
        report.write_bytes(sentinel.read_bytes())
        private_file(report)
    elif kind == 'file':
        report.write_bytes(sentinel.read_bytes())
    elif kind == 'symlink':
        report.symlink_to(sentinel)
    elif kind == 'hardlink':
        os.link(sentinel, report)
    else:
        report.mkdir()
    smoke = ModuleType('agent_test.scripts.packaged_smoke')
    smoke.run = lambda path: pytest.fail('已有报告路径必须在运行自检前拒绝')
    monkeypatch.setitem(sys.modules, 'agent_test.scripts.packaged_smoke', smoke)
    monkeypatch.setattr(sys, 'argv', [sys.executable, '--self-check', str(report)])
    with pytest.raises(SystemExit) as result:
        launcher.main()
    assert result.value.code != 0
    assert sentinel.read_bytes() == b'PRIVATE_USER=preserve\n'
    if kind != 'directory':
        assert report.read_bytes() == b'PRIVATE_USER=preserve\n'
    assert calls == []


def test_report_concurrent_processes_publish_one_complete_json(tmp_path):
    report = tmp_path / 'report.json'
    script = '''
import sys
from pathlib import Path
from agent_frontend.desktop.launcher import _write_info
try:
    _write_info(Path(sys.argv[1]), {'writer': int(sys.argv[2]), 'payload': '完整' * 1000})
except FileExistsError:
    raise SystemExit(17)
'''
    processes = [subprocess.Popen([sys.executable, '-c', script, str(report), str(index)],
                                 cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for index in range(4)]
    codes = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode in (0, 17), stderr
            codes.append(process.returncode)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert codes.count(0) == 1
    payload = json.loads(report.read_text(encoding='utf-8'))
    assert payload['writer'] in range(4)
    assert payload['payload'] == '完整' * 1000
    assert sorted(path.name for path in tmp_path.iterdir()) == ['report.json']


def test_windows_default_root_helper_uses_executable_directory(frozen_launcher):
    launcher, install, extracted, working, local, calls = frozen_launcher
    assert launcher.frozen_data_root('win32') == install


@pytest.mark.parametrize('relative', ['config', 'data', 'data/state', 'data/workspaces', 'data/projects', 'data/projects/default',
                                     'data/raw', 'cache/rag', 'cache/huggingface', 'cache/torch',
                                     'cache/tmp', 'logs', 'cache', 'cache/huggingface/hub',
                                     'cache/huggingface/modules', 'cache/sentence-transformers', 'cache/fastembed'])
def test_each_managed_directory_is_actually_writable(portable, tmp_path, monkeypatch, relative):
    root = tmp_path / 'portable'
    portable.initialize_layout(root)
    target = root / relative
    old_mode = target.stat().st_mode
    original = portable.tempfile.mkstemp
    def denied(*args, **kwargs):
        if Path(kwargs.get('dir', '')) == target:
            raise PermissionError(errno.EACCES, 'controlled managed directory denial')
        return original(*args, **kwargs)
    monkeypatch.setattr(portable.tempfile, 'mkstemp', denied)
    with pytest.raises(portable.PortableError) as error:
        portable.initialize_layout(root)
    assert error.value.kind == 'unwritable'
    assert error.value.path == target
    assert error.value.operation
    assert target.stat().st_mode == old_mode


def test_valid_multiline_dotenv_preserves_exact_bytes(portable, tmp_path):
    root = tmp_path / 'portable'
    root.mkdir()
    content = b'PROMPT="first line\nsecond line"\nTOOL_PERMISSION_POLICY=readonly\n'
    env = root / '.env'
    env.write_bytes(content)
    private_file(env)
    portable.initialize_layout(root)
    assert env.read_bytes() == content
    assert 'PROMPT' not in os.environ


@pytest.mark.skipif(os.name != 'nt', reason='Windows 句柄 file_only 发布，必须由 Native 专项执行')
@pytest.mark.parametrize('kind', ['directory', 'hardlink'])
def test_windows_file_only_publish_rejects_nonprivate_source(tmp_path, kind):
    from agent_core.core.windows_files import rename_noreplace
    source, target = tmp_path / 'source', tmp_path / 'target'
    if kind == 'directory':
        source.mkdir()
        sentinel = source / 'child'
        sentinel.write_bytes(b'preserve-source')
    else:
        source.write_bytes(b'preserve-source')
        sentinel = tmp_path / 'alias'
        os.link(source, sentinel)
    with pytest.raises(PermissionError):
        rename_noreplace(source, target, file_only=True)
    assert source.exists()
    assert sentinel.read_bytes() == b'preserve-source'
    assert not target.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows reparse 源拒绝，必须由 Native 专项执行')
def test_windows_file_only_publish_rejects_reparse_source(tmp_path, symlink_capable):
    from agent_core.core.windows_files import rename_noreplace
    outside = tmp_path / 'outside'
    outside.write_bytes(b'preserve-outside')
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.symlink_to(outside)
    with pytest.raises(PermissionError):
        rename_noreplace(source, target, file_only=True)
    assert source.is_symlink()
    assert outside.read_bytes() == b'preserve-outside'
    assert not target.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows 单文件发布，必须由 Native 专项执行')
def test_windows_file_only_publish_moves_regular_single_link(tmp_path):
    from agent_core.core.windows_files import rename_noreplace
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'complete-content')
    rename_noreplace(source, target, file_only=True)
    assert not source.exists()
    assert target.read_bytes() == b'complete-content'
