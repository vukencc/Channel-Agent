import copy
import threading
import time

import pytest

pytest.importorskip('PySide6.QtWidgets')

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from agent_frontend.desktop.working import WorkingPanel


def test_working_panel_interaction_and_refresh():
    app = QApplication.instance() or QApplication([])
    panel = WorkingPanel()
    panel.resize(1000, 650)
    rows = [{'run_id': 'run-1', 'title': '运行 Flow', 'status': 'running', 'nodes': [
        {'id': 'node-1', 'title': '分析', 'status': 'running', 'task_summary': '<任务>',
         'model': 'example', 'subagent_count': 2, 'active_subagent_count': 1,
         'subagents': [{'title': '子任务', 'status': 'running', 'task_summary': '整理'}],
         'handoff_validation': {'status': 'failed', 'message': '格式错误'}},
        {'id': 'node-2', 'title': '执行', 'status': 'queued'}]}]
    panel.set_runs(rows)
    panel.show()
    app.processEvents()
    item = panel.listing.item(1)
    assert '&lt;任务&gt;' in item.toolTip()
    QTest.mouseClick(panel.listing.viewport(), Qt.MouseButton.LeftButton,
                     pos=panel.listing.visualItemRect(item).center())
    assert '<任务>' in panel.details.toPlainText()
    assert '子任务' in panel.details.toPlainText()
    assert '显示 1 / 2' in panel.details.toPlainText()
    assert '状态：运行中' in panel.details.toPlainText()
    assert '校验失败：格式错误' in panel.details.toPlainText()
    assert '"title"' not in panel.details.toPlainText()
    updated = copy.deepcopy(rows)
    updated[0]['nodes'][0]['phase'] = '执行中'
    panel.set_runs(updated)
    assert panel.listing.currentItem().data(Qt.ItemDataRole.UserRole) == ('run-1', 'node-1')
    assert '执行中' in panel.details.toPlainText()
    panel.resize(400, 650)
    app.processEvents()
    assert panel.splitter.orientation() == Qt.Orientation.Vertical
    panel.set_runs([])
    assert not panel.details.toPlainText()
    assert panel.empty.isVisible()
    panel.close()


from test_desktop_flow_lifecycle import lifecycle, create_flow, wait


def test_working_navigation_with_live_bridge(lifecycle):
    app, open_window, project, _ = lifecycle
    window, bridge = open_window()
    flow = create_flow(app, window, bridge, project, '等待取消')
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait(app, lambda: ((window.state.get('selected_flow') or {}).get('run') or {}).get('status') == 'running')
    QTest.mouseClick(window.working_button, Qt.MouseButton.LeftButton)
    wait(app, lambda: bool(window.working_panel._rows))
    assert window.workspace_stack.currentWidget() is window.working_panel
    panel = window.working_panel
    item = panel.listing.item(1)
    QTest.mouseClick(panel.listing.viewport(), Qt.MouseButton.LeftButton,
                     pos=panel.listing.visualItemRect(item).center())
    assert panel._selection
    selected = panel._selection
    panel.listing.setFocus()
    panel.listing.setCurrentRow(2)
    QTest.keyClick(panel.listing, Qt.Key.Key_Return)
    assert panel._selection == (selected[0], 'next')
    panel.listing.setCurrentRow(1)
    QTest.keyClick(panel.listing, Qt.Key.Key_Return)
    assert panel._selection == selected
    assert 'working_runs' not in window.state
    calls = []
    installed = threading.Event()
    def instrument():
        original = bridge._flows.working_runs
        def counted(**kwargs):
            calls.append(time.monotonic())
            return original(**kwargs)
        bridge._flows.working_runs = counted
        installed.set()
    bridge._loop.call_soon_threadsafe(instrument)
    wait(app, installed.is_set)
    for _ in range(20):
        bridge._notify()
        app.processEvents()
        time.sleep(.01)
    assert len(calls) <= 1
    wait(app, lambda: bool(calls), 2)
    assert panel._selection == selected
    assert panel.listing.currentItem() is not None
    window.resize(450, 700)
    app.processEvents()
    assert panel.splitter.orientation() == Qt.Orientation.Vertical
    assert panel.details.isVisible()
    window._cancel()
    assert ((window.state.get('selected_flow') or {}).get('run') or {}).get('status') == 'running'
    window._leave_working()
    assert window.workspace_stack.currentWidget() is window.flow_page
    assert window.state['selected_flow']['id'] == flow['id']
    bridge.request('flow_cancel', flow_id=flow['id'])
    wait(app, lambda: ((window.state.get('selected_flow') or {}).get('run') or {}).get('status') == 'cancelled')
