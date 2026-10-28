"""原生窗口与真实 Core Flow 的集成；模型回复为明确受控回调。"""
import asyncio
import copy
import json
import time

import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
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
        QTest.qWait(10)
    assert predicate(), '等待 Flow 窗口超时'


@pytest.fixture
def flow_desktop(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'HANDOFF.md').write_text('初始交接：验证真实项目传递', encoding='utf-8')
    prompts = []

    async def model(history, **kwargs):
        prompts.append(copy.deepcopy(history))
        if history[0]['content'].startswith('等待取消'):
            await asyncio.sleep(60)
        return {'role': 'assistant', 'content': '已完成节点，交接标记：next-step'}

    bridge = RuntimeBridge(tmp_path / 'state', workspace_root=tmp_path / 'workspace', model=model)
    window = MainWindow(bridge)
    window.show()
    bridge.start()
    wait(app, lambda: window.current_session is not None)
    yield app, window, bridge, project, prompts
    window.close()
    wait(app, lambda: not bridge.is_running and not bridge.worker_alive, 15)
    window.deleteLater()
    bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def create_flow(app, window, bridge, project):
    bridge.request('flow_create', title='有向交接项目', workspace_path=str(project))
    wait(app, lambda: bool(window.state.get('selected_flow')))
    flow = copy.deepcopy(window.state['selected_flow'])
    flow['nodes'] = [
        {'id': 'first', 'title': '准备', 'prompt': '先检查项目', 'x': 0, 'y': 0,
         'handoff_file': 'HANDOFF.md'},
        {'id': 'next', 'title': '验证', 'prompt': '再验证项目', 'x': 280, 'y': 0},
    ]
    flow['edges'] = [{'source': 'first', 'target': 'next'}]
    return flow


def test_flow_creation_run_handoff_and_classic_mode_are_independent(flow_desktop):
    app, window, bridge, project, prompts = flow_desktop
    classic = window.current_session['id']
    flow = create_flow(app, window, bridge, project)
    assert window.workspace_stack.currentWidget() is window.flow_page
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait(app, lambda: (window.state.get('selected_flow', {}).get('run') or {}).get('status') == 'completed')
    run = window.state['selected_flow']['run']
    assert '初始交接' in run['nodes']['first']['received_handoff']
    assert 'next-step' in run['nodes']['next']['received_handoff']
    assert len(prompts) == 2
    assert len(window.state['flows']) == 1
    bridge.request('select', session_id=classic)
    wait(app, lambda: window.state.get('selected_flow') is None)
    assert window.workspace_stack.currentWidget() is window.agent_page
    assert window.current_session['id'] == classic
    bridge.request('flow_select', flow_id=flow['id'])
    wait(app, lambda: bool(window.state.get('selected_flow')))
    assert window.state['selected_flow']['run']['status'] == 'completed'


def test_flow_cancel_blocks_successors_and_keeps_graph(flow_desktop):
    app, window, bridge, project, _ = flow_desktop
    flow = create_flow(app, window, bridge, project)
    flow['nodes'][0]['prompt'] = '等待取消'
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait(app, lambda: (window.state['selected_flow'].get('run') or {}).get('status') == 'running')
    QTest.mouseClick(window.flow_panel.run_button, Qt.MouseButton.LeftButton)
    wait(app, lambda: window.state['selected_flow']['run']['status'] == 'cancelled')
    assert window.state['selected_flow']['run']['nodes']['next']['status'] == 'blocked'
    assert len(window.state['selected_flow']['nodes']) == 2


def test_new_project_dialog_selects_agent_or_flow(flow_desktop):
    from agent_frontend.desktop.project_dialog import ProjectDialog
    _, window, _, project, _ = flow_desktop
    dialog = ProjectDialog(window)
    dialog.title.setText('项目名')
    dialog.mode.setCurrentIndex(dialog.mode.findData('flow'))
    dialog.workspace.setText(str(project))
    dialog.handoff.setPlainText('用户输入交接')
    dialog.accept()
    assert dialog.result_payload['mode'] == 'flow'
    assert dialog.result_payload['handoff'] == '用户输入交接'
    dialog.deleteLater()
