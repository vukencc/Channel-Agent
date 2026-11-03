"""真实桌面桥接与受控模型：延迟审计期间 Flow 快照先于操作确认。"""
import copy
import json
import threading
import time

import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from agent_core import config
from agent_frontend.desktop.runtime import RuntimeBridge
from agent_frontend.desktop.window import MainWindow


def wait(app, predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), '等待桌面状态超时'


def test_late_flow_run_ack_keeps_completed_panel_state(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    project = tmp_path / 'project'
    project.mkdir()

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': '受控节点已完成'}

    bridge = RuntimeBridge(tmp_path / 'state', workspace_root=tmp_path / 'workspace', model=model)
    window = MainWindow(bridge)
    entered, release = threading.Event(), threading.Event()
    acknowledgements = []
    bridge.finished.connect(lambda action, payload: acknowledgements.append(json.loads(payload))
                            if action == 'flow_run' else None)
    original_audit = bridge._audit

    def delayed_audit(event, **fields):
        if event == 'flow_run':
            entered.set()
            if not release.wait(15):
                raise TimeoutError('测试审计门未释放')
        return original_audit(event, **fields)

    monkeypatch.setattr(bridge, '_audit', delayed_audit)
    window.show()
    bridge.start()
    try:
        wait(app, lambda: window.current_session is not None)
        bridge.request('flow_create', title='确认时序模板')
        wait(app, lambda: bool(window.state.get('selected_flow')))
        flow = copy.deepcopy(window.state['selected_flow'])
        flow['nodes'] = [{'id': 'first', 'title': '节点', 'prompt': '完成节点', 'x': 0, 'y': 0}]
        bridge.request('flow_save', definition=flow)
        wait(app, lambda: window.state['selected_flow']['revision'] > flow['revision'])
        bridge.request('project_create', title='确认时序项目', workspace_path=str(project), flow_id=flow['id'])
        wait(app, lambda: bool((window.state.get('selected_flow') or {}).get('project_id')))
        window.flow_panel._submit('flow_run')
        wait(app, entered.is_set)
        wait(app, lambda: ((window.state.get('selected_flow') or {}).get('run') or {}).get('status') == 'completed')
        run = window.state['selected_flow']['run']
        assert window.flow_panel._flow['run']['id'] == run['id']
        assert window.flow_panel._flow['run']['status'] == 'completed'
        assert not acknowledgements
        release.set()
        wait(app, lambda: bool(acknowledgements))
        assert window.state['selected_flow']['run']['status'] == 'completed'
        assert window.flow_panel._flow['run']['id'] == run['id']
        assert window.flow_panel._flow['run']['status'] == 'completed'
        assert acknowledgements[-1]['run']['id'] == run['id']
        assert acknowledgements[-1]['run']['status'] == 'completed'
    finally:
        release.set()
        window.close()
        wait(app, lambda: not bridge.is_running and not bridge.worker_alive, 15)
        window.deleteLater()
        bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
