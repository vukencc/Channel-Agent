"""Source Qt 离屏受控回归；不代表真实 exe 可见窗口或人工交互验收。"""
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile

import pytest

from test_windows_portable_acl import NativeAcl


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='需要 Windows 实际 token、NTFS ACL 与离屏 Qt')


@pytest.fixture
def controlled_bootstrap(tmp_path, monkeypatch, sandbox_env):
    """仅模拟 frozen 元数据和公开资源，初始化与权限检查均执行真实源码。"""
    for name in ('AI_AGENT_PROJECT_ROOT', 'AI_AGENT_PORTABLE', 'HF_HOME', 'TORCH_HOME',
                 'XDG_CACHE_HOME', 'TEMP', 'TMP', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE',
                 'TRANSFORMERS_CACHE', 'HF_MODULES_CACHE', 'SENTENCE_TRANSFORMERS_HOME',
                 'FASTEMBED_CACHE_PATH'):
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, 'tempdir', tempfile.tempdir)
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from agent_frontend.desktop import launcher
    app = QApplication.instance() or QApplication(['controlled-portable-dialog'])
    assert app.platformName() == 'offscreen', '禁止在系统可见 Qt 平台执行受控交互'
    monkeypatch.setattr(sys, 'argv', ['controlled-ChannelAgent.exe'])
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    extracted = tmp_path / 'controlled-extracted'
    resources = extracted / 'portable_resources'
    resources.mkdir(parents=True)
    for relative, content in {
        'configuration.example.env': b'# controlled public template\n',
        'LICENSE.txt': b'controlled public license\n',
        '使用说明.txt': '受控公开资料\n'.encode(),
        'WSL/requirements.txt': b'# controlled lock\n',
        'THIRD_PARTY_LICENSES/index.json': b'{}\n',
    }.items():
        target = resources / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    monkeypatch.setattr(sys, '_MEIPASS', str(extracted), raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'isolated-local'))
    monkeypatch.delenv('AI_AGENT_PROJECT_ROOT', raising=False)
    monkeypatch.delenv('AI_AGENT_PORTABLE', raising=False)
    working = tmp_path / 'unrelated-cwd'
    working.mkdir()
    monkeypatch.chdir(working)
    acl = NativeAcl()
    with acl.current_user_default_owner():
        root = tmp_path / 'controlled-exe'
        root.mkdir()
        executable = root / 'ChannelAgent.exe'
        executable.write_bytes(b'controlled frozen metadata, not executable')
        monkeypatch.setattr(sys, 'executable', str(executable))
        try:
            yield launcher, app, root, acl
        finally:
            acl.apply(root, f'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{acl.current_sid})')
            assert not list(working.iterdir())


@contextmanager
def activate_message_button(app, text: str):
    """点击真实 QMessageBox 按钮；两秒兜底关闭当前受控消息框，失败仍为红测。"""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox
    state = {'clicked': [], 'timeout': False}
    poll = QTimer()
    watchdog = QTimer()
    watchdog.setSingleShot(True)

    def click() -> None:
        box = app.activeModalWidget()
        if not isinstance(box, QMessageBox):
            return
        for button in box.buttons():
            expected = (box.standardButton(button) == QMessageBox.StandardButton.Ok
                        if text == 'standard-ok' else button.text().replace('&', '') == text)
            if expected:
                state['clicked'].append(text)
                poll.stop()
                button.click()
                return

    def expire() -> None:
        state['timeout'] = True
        poll.stop()
        for box in app.topLevelWidgets():
            if isinstance(box, QMessageBox):
                box.reject()

    poll.timeout.connect(click)
    watchdog.timeout.connect(expire)
    poll.start(10)
    watchdog.start(2000)
    try:
        yield state
    finally:
        poll.stop()
        watchdog.stop()
        poll.timeout.disconnect(click)
        watchdog.timeout.disconnect(expire)
    assert not state['timeout'], '两秒内未找到并点击预期的真实消息框按钮'
    assert state['clicked'] == [text]


@pytest.mark.parametrize('choice', ['user', 'custom', 'cancel', 'custom-cancel'])
def test_source_qt_readonly_fallback_buttons(controlled_bootstrap, tmp_path, monkeypatch, choice):
    from PySide6.QtWidgets import QFileDialog
    from agent_core import config
    from agent_core.core.windows_security import verify_private_path
    launcher, app, root, acl = controlled_bootstrap
    policy = config.TOOL_PERMISSION_POLICY
    acl.apply(root, f'D:P(D;;0x6;;;{acl.current_sid})(A;;FA;;;SY)(A;;FA;;;{acl.current_sid})')
    with pytest.raises(PermissionError):
        (root / 'actual-write-probe').write_bytes(b'probe')
    manual = tmp_path / '手选 数据根'
    selected = tmp_path / 'isolated-local/ChannelAgentPortable' if choice == 'user' else manual
    button = {'user': '使用用户目录', 'custom': '选择目录', 'cancel': '取消', 'custom-cancel': '选择目录'}[choice]
    file_dialog_calls = []

    def choose_directory(*args, **kwargs) -> str:
        file_dialog_calls.append(True)
        return '' if choice == 'custom-cancel' else str(manual)

    # 仅系统目录选择器被替换；消息框选择和启动分支均保留实际实现。
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', choose_directory)
    with activate_message_button(app, button):
        if choice in ('cancel', 'custom-cancel'):
            with pytest.raises(SystemExit) as error:
                launcher._portable_bootstrap()
            assert error.value.code != 0
        else:
            launcher._portable_bootstrap()
    assert file_dialog_calls == ([True] if choice.startswith('custom') else [])
    assert not (root / '.env').exists()
    assert config.TOOL_PERMISSION_POLICY == policy == 'standard'
    if choice in ('cancel', 'custom-cancel'):
        assert not manual.exists()
        assert not (tmp_path / 'isolated-local/ChannelAgentPortable').exists()
        assert 'AI_AGENT_PROJECT_ROOT' not in os.environ
        assert 'AI_AGENT_PORTABLE' not in os.environ
        return
    assert os.environ['AI_AGENT_PROJECT_ROOT'] == str(selected)
    assert os.environ['AI_AGENT_PORTABLE'] == '1'
    for relative in ('config', 'data/state', 'data/workspaces', 'data/projects/default', 'data/raw', 'cache/rag'):
        assert (selected / relative).is_dir()
        verify_private_path(selected / relative)
    verify_private_path(selected / '.env')
    assert (selected / '.env').read_bytes().startswith(b'# ChannelAgent portable')
    for name, relative in {'HF_HOME': 'cache/huggingface', 'TORCH_HOME': 'cache/torch',
                           'HF_HUB_CACHE': 'cache/huggingface/hub', 'TEMP': 'cache/tmp',
                           'TMP': 'cache/tmp', 'XDG_CACHE_HOME': 'cache'}.items():
        assert os.environ[name] == str(selected / relative)
    assert tempfile.tempdir == str(selected / 'cache/tmp')
    assert (selected / 'LICENSE.txt').read_bytes() == b'controlled public license\n'
    assert (selected / 'WSL/requirements.txt').read_bytes() == b'# controlled lock\n'


def test_source_qt_unsafe_existing_env_errors_without_fallback(controlled_bootstrap, tmp_path):
    from agent_core.core.windows_security import verify_private_path
    launcher, app, root, acl = controlled_bootstrap
    env = root / '.env'
    content = b'CONTROLLED_PRIVATE_SENTINEL=preserve\nTOOL_PERMISSION_POLICY=readonly\n'
    env.write_bytes(content)
    acl.apply(env, f'D:P(A;;FA;;;SY)(A;;FA;;;{acl.current_sid})(A;;GR;;;AU)')
    with pytest.raises(PermissionError):
        verify_private_path(env)
    with activate_message_button(app, 'standard-ok'):
        with pytest.raises(SystemExit) as error:
            launcher._portable_bootstrap()
    assert error.value.code != 0
    assert env.read_bytes() == content
    with pytest.raises(PermissionError):
        verify_private_path(env)
    assert 'AI_AGENT_PROJECT_ROOT' not in os.environ
    assert 'AI_AGENT_PORTABLE' not in os.environ
    assert not (tmp_path / 'isolated-local/ChannelAgentPortable').exists()
