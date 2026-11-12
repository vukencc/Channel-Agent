"""工作区切换和导航入口使用真实 Qt 事件，不启动模型。"""
import json
import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtCore import QObject, QCoreApplication, QEvent, Signal, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from agent_frontend.desktop.window import MainWindow


class WorkspaceBridge(QObject):
    state_changed = Signal(str)
    working_changed = Signal(str)
    error = Signal(str)
    finished = Signal(str, str)
    stopped = Signal()
    is_running = False

    def __init__(self):
        super().__init__()
        self.requests = []

    def request(self, action, **payload):
        self.requests.append((action, payload))


@pytest.fixture
def workspace_window():
    app = QApplication.instance() or QApplication([])
    bridge = WorkspaceBridge()
    window = MainWindow(bridge)
    window.show()
    app.processEvents()
    yield app, window, bridge
    window.close()
    window.deleteLater()
    bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_workspace_pages_use_their_own_toolbar(workspace_window):
    app, window, _ = workspace_window
    header = window.findChild(QWidget, 'header')
    assert header.isVisible()
    for page in (window.flow_page, window.working_panel):
        window.workspace_stack.setCurrentWidget(page)
        app.processEvents()
        assert not header.isVisible(), 'Flow/Working 不应叠加会话顶栏'
    window.workspace_stack.setCurrentWidget(window.agent_page)
    app.processEvents()
    assert header.isVisible()


@pytest.mark.parametrize('page_name', ['flow_panel', 'working_panel'])
def test_hidden_navigation_can_be_restored_from_workspace_toolbar(workspace_window, page_name):
    app, window, _ = workspace_window
    page = getattr(window, page_name)
    window.workspace_stack.setCurrentWidget(window.flow_page if page_name == 'flow_panel' else page)
    window.sidebar.hide()
    app.processEvents()
    QTest.mouseClick(page.sidebar_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert window.sidebar.isVisible()
    QTest.mouseClick(page.sidebar_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not window.sidebar.isVisible()


def test_saved_model_is_used_when_environment_default_is_unconfigured(workspace_window):
    app, window, bridge = workspace_window
    state = {'sessions': [], 'models': [{'id': 'runtime-saved', 'name': '已保存模型'}],
             'selected': None, 'selected_flow': None, 'flows': [], 'projects': [],
             'default_model': None, 'default_model_available': False,
             'runtime': {'active_agents': 0, 'active_model_calls': 0, 'active_tools': []}}
    window._receive_state(json.dumps(state))
    window._open_flow_designer()
    app.processEvents()
    designer = window.flow_panel.designer
    assert designer.model.currentData() == 'runtime-saved'
    designer.input.setText('创建反馈分流模板')
    designer.submit()
    assert bridge.requests[-1][0] == 'flow_design'
    assert bridge.requests[-1][1]['model_profile'] == 'runtime-saved'


def test_designer_failure_is_shown_once_in_its_context(workspace_window):
    app, window, _ = workspace_window
    window.state['selected_flow'] = {'id': 'f'}
    window.workspace_stack.setCurrentWidget(window.flow_page)
    designer = window.flow_panel.designer
    designer.input.setText('创建模板')
    designer.submit()
    identifier = designer.request_id
    window._show_error('设计连接失败')
    app.processEvents()
    assert window.flow_error.isVisible()
    window._finished('flow_design', json.dumps({'request_id': identifier, 'error': '设计连接失败'}))
    assert '设计连接失败' in designer.transcript.toPlainText()
    assert not window.flow_error.isVisible()
