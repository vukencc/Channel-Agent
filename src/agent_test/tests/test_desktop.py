"""真实 Qt 控件与 Core 运行时回归；模型仅为明确的受控回调。"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QUrl
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from agent_core import config
from agent_core.api import SessionStore
from agent_frontend.desktop.runtime import RuntimeBridge
from agent_frontend.desktop.window import MainWindow, ModelDialog, SessionDialog


@pytest.fixture(scope='module')
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def wait_until(qt_app, predicate, timeout=6000):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), '等待原生界面状态超时'


@pytest.fixture
def desktop(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    owner_threads = []

    async def controlled_model(history, *, session_id, emit):
        owner_threads.append(threading.get_ident())
        prompt = history[-1].get('content', '')
        if prompt == '确认写入':
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'controlled-write', 'function': {'name': 'create_file',
                'arguments': json.dumps({'path': 'approval.txt', 'content': 'approved'})}}]}
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': '工具已返回'}
        emit('content', '第一段')
        await asyncio.sleep(0.15)
        if prompt == '等待取消':
            await asyncio.sleep(60)
        emit('content', '，第二段')
        return {'role': 'assistant', 'content': '第一段，第二段'}

    bridge = RuntimeBridge(tmp_path / 'state', workspace_root=tmp_path / 'workspace', model=controlled_model)
    window = MainWindow(bridge)
    window.show()
    bridge.start()
    wait_until(qt_app, lambda: window.current_session is not None)
    yield window, bridge, owner_threads
    window.close()
    wait_until(qt_app, lambda: not bridge.is_running and not bridge.worker_alive, timeout=10000)
    assert not bridge.worker_alive
    window.deleteLater()
    bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_native_send_stream_cancel_and_thread_switch(qt_app, desktop):
    window, bridge, owner_threads = desktop
    first = window.current_session['id']
    window.composer.setPlainText('等待取消')
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    wait_until(qt_app, lambda: '第一段' in window.transcript.toPlainText())
    assert window.stop_button.isEnabled()
    assert not window.send_button.isEnabled()
    bridge.request('create', title='第二个会话')
    wait_until(qt_app, lambda: window.current_session['id'] != first)
    bridge.request('select', session_id=first)
    wait_until(qt_app, lambda: window.current_session['id'] == first)
    QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
    wait_until(qt_app, lambda: not window.current_session['busy'])
    assert window.current_session['status'] == 'cancelled'
    assert owner_threads and all(value != threading.get_ident() for value in owner_threads)


def test_confirmation_is_bound_and_close_denies(qt_app, desktop):
    window, bridge, _ = desktop
    bridge.request('settings', session_id=window.current_session['id'], permission_policy='standard')
    window.composer.setPlainText('确认写入')
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    wait_until(qt_app, lambda: window.confirmation_dialog is not None)
    window.confirmation_dialog.reject()
    wait_until(qt_app, lambda: not window.current_session['busy'])
    assert not (bridge.workspace_root / window.current_session['id'] / 'approval.txt').exists()
    assert '工具已返回' in window.transcript.toPlainText()


def test_history_scroll_retained_and_draft_survives_switch(qt_app, desktop):
    window, bridge, _ = desktop
    first = window.current_session['id']
    bridge.request('create', title='另一个会话')
    wait_until(qt_app, lambda: window.current_session['id'] != first)
    second = window.current_session['id']
    window.composer.setPlainText('保留草稿')
    bridge.request('select', session_id=first)
    wait_until(qt_app, lambda: window.current_session['id'] == first)
    bridge.request('select', session_id=second)
    wait_until(qt_app, lambda: window.current_session['id'] == second)
    assert window.composer.toPlainText() == '保留草稿'
    messages = [{'role': 'assistant', 'content': '历史内容\n' * 100}]
    window.render_transcript(messages, '', session_id=second)
    qt_app.processEvents()
    bar = window.transcript.verticalScrollBar()
    bar.setValue(80)
    window.render_transcript(messages, '新增内容', session_id=second)
    qt_app.processEvents()
    assert bar.value() == 80


def test_model_key_password_clear_and_public_snapshot(qt_app, desktop):
    window, bridge, _ = desktop
    secret = 'desktop-private-key-123456'
    dialog = ModelDialog(window)
    assert dialog.api_key.echoMode() == QLineEdit.EchoMode.Password
    dialog.api_key.setText(secret)
    dialog.reject()
    assert dialog.api_key.text() == ''
    snapshots = []
    bridge.state_changed.connect(snapshots.append)
    bridge.request('model_add', name='本机连接', model='controlled', base_url='https://example.invalid/v1', api_key=secret)
    wait_until(qt_app, lambda: any(row['name'] == '本机连接' for row in window.state['models']))
    assert all(secret not in item for item in snapshots)
    row = next(row for row in window.state['models'] if row['name'] == '本机连接')
    bridge.request('settings', session_id=window.current_session['id'], title=secret)
    wait_until(qt_app, lambda: window.current_session['title'] == '[已隐藏配置密钥]')
    bridge.request('model_delete', model_id=row['id'])
    wait_until(qt_app, lambda: all(row['name'] != '本机连接' for row in window.state['models']))
    assert secret not in window.title_label.text()
    assert all(secret not in item for item in snapshots)


def test_shutdown_drains_running_task_and_releases_store(qt_app, desktop):
    window, bridge, _ = desktop
    window.composer.setPlainText('等待取消')
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    wait_until(qt_app, lambda: window.current_session['busy'])
    window.close()
    wait_until(qt_app, lambda: not bridge.is_running, timeout=10000)
    with_store = SessionStore(bridge.state_dir, workspace_root=bridge.workspace_root)
    try:
        assert with_store.load_all()[0]['status'] == 'cancelled'
    finally:
        with_store.close()


def test_untrusted_message_never_loads_local_or_remote_resource(qt_app, desktop, tmp_path):
    window, _, _ = desktop
    private = tmp_path / 'private.svg'
    private.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    content = f'<img src="{private.as_uri()}"> ![remote](https://example.invalid/pixel.png)'
    window.render_transcript([{'role': 'assistant', 'content': content}], '', session_id=window.current_session['id'])
    assert content in window.transcript.toPlainText()
    for url in [private.as_uri(), 'https://example.invalid/pixel.png']:
        assert window.transcript.loadResource(QTextDocument.ResourceType.ImageResource, QUrl(url)).isEmpty()
    assert not window.transcript.openLinks()
    assert not window.transcript.openExternalLinks()


def test_settings_preserve_legacy_policy_and_model_key_on_edit(qt_app, desktop):
    window, bridge, _ = desktop
    session_id = window.current_session['id']
    bridge.request('settings', session_id=session_id, permission_policy='standard', title='保留严格确认')
    wait_until(qt_app, lambda: window.current_session['permission_policy'] == 'standard')
    dialog = SessionDialog(window, window.current_session, window.state['models'], '默认')
    dialog.title.setText('仅改标题')
    dialog.accept()
    assert dialog.result_payload['permission_policy'] == 'standard'
    bridge.request('settings', session_id=session_id, **dialog.result_payload)
    wait_until(qt_app, lambda: window.current_session['title'] == '仅改标题')
    assert window.current_session['permission_policy'] == 'standard'
    bridge.request('model_add', name='待编辑', model='controlled', base_url='https://example.invalid/v1', api_key='private-edit-secret-12345')
    wait_until(qt_app, lambda: any(row['name'] == '待编辑' for row in window.state['models']))
    row = next(row for row in window.state['models'] if row['name'] == '待编辑')
    editor = ModelDialog(window, row)
    assert editor.api_key.text() == ''
    editor.name.setText('编辑完成')
    editor.accept()
    bridge.request('model_edit', model_id=row['id'], **editor.result_payload)
    wait_until(qt_app, lambda: any(row['name'] == '编辑完成' for row in window.state['models']))
    edited = next(row for row in window.state['models'] if row['name'] == '编辑完成')
    assert edited['has_api_key'] and 'api_key' not in edited
    assert all('tool_names' not in row for row in window.state['sessions'])


def test_saved_history_is_paginated_and_fully_reachable(qt_app, tmp_path):
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    record = store.create('长历史', 'system')
    record['messages'].extend({'role': 'assistant', 'content': f'第 {index} 条消息'} for index in range(145))
    store.save(record)
    store.close()
    bridge = RuntimeBridge(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    window = MainWindow(bridge)
    window.show()
    bridge.start()
    try:
        wait_until(qt_app, lambda: window.current_session is not None)
        assert len(window.current_session['messages']) == 60
        assert window.current_session['next_before'] > 0
        QTest.mouseClick(window.older_button, Qt.MouseButton.LeftButton)
        wait_until(qt_app, lambda: len(window.current_session['messages']) == 120)
        QTest.mouseClick(window.older_button, Qt.MouseButton.LeftButton)
        wait_until(qt_app, lambda: not window.current_session['next_before'])
        assert '第 0 条消息' in window.transcript.toPlainText()
        assert '第 144 条消息' in window.transcript.toPlainText()
    finally:
        window.close()
        wait_until(qt_app, lambda: not bridge.worker_alive, timeout=10000)
        window.deleteLater()
        bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_native_file_preview_refuses_symlink_and_escape(qt_app, desktop, tmp_path):
    window, bridge, _ = desktop
    replies = []
    errors = []
    bridge.finished.connect(lambda action, payload: replies.append((action, json.loads(payload))))
    bridge.error.connect(errors.append)
    window._files()
    wait_until(qt_app, lambda: any(action == 'files' for action, _ in replies))
    root = bridge.workspace_root / window.current_session['id']
    (root / 'hello.txt').write_text('原生文件预览', encoding='utf-8')
    private = tmp_path / 'outside.txt'
    private.write_text('不应读取')
    (root / 'linked.txt').symlink_to(private)
    bridge.request('files', session_id=window.current_session['id'], path='.')
    wait_until(qt_app, lambda: any(action == 'files' and any(row['name'] == 'hello.txt' for row in reply.get('entries', [])) for action, reply in replies))
    assert all(row['name'] != 'linked.txt' for action, reply in replies if action == 'files' for row in reply.get('entries', []))
    bridge.request('file', session_id=window.current_session['id'], path='hello.txt')
    wait_until(qt_app, lambda: window.file_preview.toPlainText() == '原生文件预览')
    for path in ['linked.txt', '../outside.txt', private.as_posix()]:
        count = len(errors)
        bridge.request('file', session_id=window.current_session['id'], path=path)
        wait_until(qt_app, lambda: len(errors) > count)
    assert window.file_preview.toPlainText() == '原生文件预览'


def test_invalid_settings_do_not_partially_change_permission(qt_app, desktop):
    window, bridge, _ = desktop
    errors = []
    bridge.error.connect(errors.append)
    before = window.current_session.copy()
    bridge.request('settings', session_id=before['id'], title='  ', permission_policy='readonly')
    wait_until(qt_app, lambda: bool(errors))
    QTest.qWait(150)
    assert window.current_session['title'] == before['title']
    assert window.current_session['permission_policy'] == before['permission_policy']
    bridge.request('settings', session_id=before['id'], model_profile='missing-profile', permission_policy='readonly')
    wait_until(qt_app, lambda: len(errors) == 2)
    QTest.qWait(150)
    assert window.current_session['permission_policy'] == before['permission_policy']


def test_entrypoint_native_teardown_exits_cleanly_without_web(tmp_path):
    script = '''
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from agent_frontend.desktop.app import main
app = QApplication([])
def close_window():
    for window in app.topLevelWidgets():
        window.close()
QTimer.singleShot(600, close_window)
main()
assert 'fastapi' not in sys.modules
assert 'PySide6.QtWebEngineWidgets' not in sys.modules
'''
    environment = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'DEBUG': 'false', 'ENABLE_DEBUG_TOOL': 'false'}
    result = subprocess.run([sys.executable, '-c', script, '--state-dir', str(tmp_path / 'state')],
                            env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_dynamic_labels_never_interpret_html(qt_app, desktop, tmp_path):
    window, bridge, _ = desktop
    title = '<img src="/tmp/private.svg">'
    bridge.request('settings', session_id=window.current_session['id'], title=title)
    wait_until(qt_app, lambda: window.current_session['title'] == title)
    assert window.title_label.text() == title
    for label in [window.title_label, window.workspace_label, window.phase_label, window.error_label,
                  window.model_label, window.file_path_label]:
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_stale_confirmation_cannot_approve_and_explicit_allow_writes(qt_app, desktop):
    window, bridge, _ = desktop
    errors = []
    bridge.error.connect(errors.append)
    identifier = window.current_session['id']
    bridge.request('settings', session_id=identifier, permission_policy='standard')
    window.composer.setPlainText('确认写入')
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
    wait_until(qt_app, lambda: window.confirmation_dialog is not None)
    bridge.request('decide', session_id=identifier, confirmation_id='stale-confirmation', allowed=True)
    wait_until(qt_app, lambda: bool(errors))
    assert not (bridge.workspace_root / identifier / 'approval.txt').exists()
    assert window.confirmation_dialog is not None
    window.confirmation_dialog.accept()
    wait_until(qt_app, lambda: not window.current_session['busy'])
    assert (bridge.workspace_root / identifier / 'approval.txt').read_text() == 'approved'


def test_native_two_agents_run_independently(qt_app, desktop):
    window, bridge, _ = desktop
    first = window.current_session['id']
    bridge.request('send', session_id=first, text='等待取消')
    wait_until(qt_app, lambda: window.current_session['busy'])
    bridge.request('create', title='第二个并发 Agent')
    wait_until(qt_app, lambda: window.current_session['id'] != first)
    second = window.current_session['id']
    bridge.request('send', session_id=second, text='等待取消')
    wait_until(qt_app, lambda: window.state['runtime']['active_model_calls'] == 2)
    bridge.request('cancel', session_id=first)
    wait_until(qt_app, lambda: any(row['id'] == first and not row['busy'] for row in window.state['sessions']))
    assert window.current_session['id'] == second and window.current_session['busy']
    bridge.request('cancel', session_id=second)
    wait_until(qt_app, lambda: window.state['runtime']['active_agents'] == 0)


def test_busy_state_directory_fails_without_hanging_or_unlocking_owner(qt_app, tmp_path):
    held = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    bridge = RuntimeBridge(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    window = MainWindow(bridge)
    window.show()
    bridge.start()
    try:
        wait_until(qt_app, lambda: not bridge.worker_alive and bool(window.error_label.text()))
        assert '状态目录已被其他进程占用' in window.error_label.text()
        with pytest.raises(RuntimeError, match='已有 CLI'):
            SessionStore(held.root)
    finally:
        window.close()
        held.close()
        window.deleteLater()
        bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
