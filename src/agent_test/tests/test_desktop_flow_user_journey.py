"""真实 Qt/Core 用户路径；模型仅为离线受控回调，不代表真实供应商执行。"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.model_settings import active_endpoint, active_profile, active_reasoning_effort
from agent_core.core.runtime_models import RuntimeModelRegistry
from agent_core.api import SessionManager
from agent_frontend.desktop.runtime import RuntimeBridge
from agent_frontend.desktop.app import configure_fonts
from agent_frontend.desktop.window import MainWindow


def _wait(app, predicate, *, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), '受控桌面路径未在原有等待期限内完成'


@pytest.fixture
def journey(tmp_path, monkeypatch, sandbox_env):
    app = QApplication.instance() or QApplication([])
    configure_fonts(app)
    for name in ('MODEL', 'BASE_URL', 'API_KEY'):
        monkeypatch.setattr(config, name, None)
    for name in ('MODEL', 'BASE_URL', 'OPENCODE_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    state = tmp_path / 'state'
    saved = RuntimeModelRegistry(state).add(name='离线受控模型', model='fixture-model',
                                          base_url='https://fixture.invalid/v1', api_key='dummy-offline-key')
    control = SimpleNamespace(started=threading.Event(), release=threading.Event(),
                              cancelled=threading.Event(), returned=threading.Event(), calls=[], mode='success')
    template = {'format': 'agent-flow', 'version': 1, 'title': '三节点受控设计',
                'model_profile': saved['id'], 'permission_policy': 'readonly',
                'nodes': [{'id': name, 'title': title, 'prompt': '核对任务：' + title,
                           'model_profile': saved['id'], 'tool_names': ['read_file', 'list_files']}
                          for name, title in [('prepare', '准备'), ('review', '复核'), ('deliver', '交付')]],
                'edges': [{'source': 'prepare', 'target': 'review'}, {'source': 'review', 'target': 'deliver'}]}

    async def model(history, **kwargs):
        control.calls.append({'profile': active_profile.get(), 'effort': active_reasoning_effort.get(),
                              'endpoint': active_endpoint.get(), 'history': history, 'kwargs': kwargs})
        control.started.set()
        try:
            while not control.release.is_set():
                await asyncio.sleep(.01)
        except asyncio.CancelledError:
            control.cancelled.set()
            if control.mode != 'late':
                raise
        if control.mode == 'failure':
            raise ConnectionError('受控连接失败')
        control.returned.set()
        return {'role': 'assistant', 'content': json.dumps({'reply': '三节点已设计', 'template': template}, ensure_ascii=False)}

    bridge = RuntimeBridge(state, workspace_root=sandbox_env, model=model)
    window = MainWindow(bridge)
    window.resize(1440, 900)
    window.show()
    bridge.start()
    try:
        _wait(app, lambda: window.current_session is not None)
        assert isinstance(bridge._manager, SessionManager)
        assert isinstance(bridge._flows, FlowService)
        window._open_flow_designer()
        app.processEvents()
        yield SimpleNamespace(app=app, window=window, bridge=bridge, control=control,
                              saved=saved, tmp_path=tmp_path)
    finally:
        control.release.set()
        # 退出时保留生产的草稿询问流程，受控选择放弃仅用于清理本测试临时状态。
        monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Discard)
        bridge.shutdown()
        _wait(app, lambda: not bridge.is_running and not bridge.worker_alive, seconds=15)
        window.close()
        window.deleteLater()
        bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _submit(journey):
    designer = journey.window.flow_panel.designer
    assert designer.model.currentData() == journey.saved['id']
    assert designer.model.currentText() == '离线受控模型'
    assert not journey.window.state['default_model_available']
    designer.effort.setCurrentIndex(designer.effort.findData('xhigh'))
    designer.input.setText('设计准备、复核、交付三个节点，只读核对任务')
    QTest.mouseClick(designer.send, Qt.MouseButton.LeftButton)
    _wait(journey.app, journey.control.started.is_set)
    call = journey.control.calls[-1]
    assert call['profile'] == journey.saved['id']
    assert call['effort'] == 'xhigh'
    assert call['endpoint'].model == 'fixture-model'
    assert call['kwargs']['session_id'] is None
    return designer


def test_saved_model_design_import_inspection_and_unsaved_draft(journey):
    app, window, control = journey.app, journey.window, journey.control
    panel = window.flow_panel
    assert not panel.inspector.isVisible()
    assert not window.findChild(QWidget, 'header').isVisible()
    designer = _submit(journey)
    request_id = designer.request_id
    QTest.mouseClick(designer.close_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not designer.isVisible() and designer.busy
    assert not control.cancelled.is_set()
    QTest.mouseClick(window.design_flow_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert designer.isVisible() and designer.request_id == request_id
    control.release.set()
    _wait(app, lambda: len((window.state.get('selected_flow') or {}).get('nodes', [])) == 3 and not designer.busy)
    flow = window.state['selected_flow']
    assert flow['permission_policy'] == 'readonly'
    assert len(flow['edges']) == 2
    assert len(journey.bridge._flows.records) == 1
    assert '三节点已设计' in designer.transcript.toPlainText()
    QTest.mouseClick(designer.close_button, Qt.MouseButton.LeftButton)
    panel._fit()
    app.processEvents()
    node = panel.nodes['prepare']
    pos = panel.canvas.mapFromScene(node.mapToScene(QPointF(80, 30)))
    QTest.mouseMove(panel.canvas.viewport(), pos)
    assert '核对任务：准备' in node.toolTip()
    assert journey.saved['name'] in node.toolTip()
    QTest.mouseClick(panel.canvas.viewport(), Qt.MouseButton.RightButton, pos=pos)
    assert panel.inspector.isVisible() and not designer.isVisible()
    panel.prompt_edit.setPlainText('尚未保存的核对规则')
    QTest.mouseClick(panel.inspector_close, Qt.MouseButton.LeftButton)
    assert not panel.inspector.isVisible() and panel.canvas.isVisible()
    QTest.mouseClick(panel.designer_button, Qt.MouseButton.LeftButton)
    assert not panel.inspector.isVisible() and designer.isVisible()
    QTest.mouseClick(designer.close_button, Qt.MouseButton.LeftButton)
    QTest.keyClick(panel.canvas, Qt.Key.Key_Menu)
    assert panel.inspector.isVisible()
    assert panel.prompt_edit.toPlainText() == '尚未保存的核对规则'
    assert panel.is_dirty
    assert flow['nodes'][0]['prompt'] == '核对任务：准备'
    for width in (1440, 500):
        window.sidebar.hide()
        window.resize(width, 900)
        app.processEvents()
        assert window.width() == width
        assert panel.canvas.isVisible()
        assert window.grab().save(str(journey.tmp_path / f'controlled-qt-journey-{width}.png'))


def test_clear_pending_design_ignores_cancellation_resistant_late_result(journey):
    journey.control.mode = 'late'
    designer = _submit(journey)
    QTest.mouseClick(designer.clear_history, Qt.MouseButton.LeftButton)
    _wait(journey.app, journey.control.returned.is_set)
    _wait(journey.app, lambda: not journey.bridge._commands)
    assert journey.control.cancelled.is_set()
    assert not designer.busy and designer.request_id is None
    assert not designer.history and not designer.transcript.toPlainText()
    assert not journey.bridge._flows.records
    assert journey.window.state.get('selected_flow') is None


@pytest.mark.parametrize('mode, expected', [('failure', '连接失败'), ('timeout', '超时')])
def test_design_failure_is_visible_without_import(journey, monkeypatch, mode, expected):
    journey.control.mode = mode
    if mode == 'timeout':
        monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', .15)
    designer = _submit(journey)
    if mode == 'failure':
        journey.control.release.set()
    _wait(journey.app, lambda: not designer.busy)
    assert expected in designer.transcript.toPlainText()
    assert designer.send.isEnabled() and not designer.stop.isEnabled()
    assert not journey.bridge._flows.records
    assert not any(row['role'] == 'assistant' for row in designer.history)
    if mode == 'timeout':
        assert journey.control.cancelled.is_set()
