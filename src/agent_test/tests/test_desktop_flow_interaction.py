import pytest
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt
from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from agent_frontend.desktop.flow import FlowPanel
from agent_frontend.desktop.flow_designer import FlowDesigner
from agent_frontend.desktop.theme import STYLE
from agent_frontend.desktop.working import WorkingPanel


def test_designer_uses_visible_configured_model_when_no_default():
    app = QApplication.instance() or QApplication([])
    designer = FlowDesigner()
    sent = []
    designer.command.connect(lambda action, payload: sent.append((action, payload)))
    designer.set_models([{}, {'id': 'a', 'name': '模型 A'}, {'id': 'b', 'name': '模型 B'}], False)
    assert designer.model.currentData() == 'a'
    assert designer.model.currentText() == '模型 A'
    designer.model.setCurrentIndex(1)
    designer.set_models([{'id': 'a'}, {'id': 'b'}], False)
    assert designer.model.currentData() == 'b'
    designer.input.setText('设计')
    designer.submit()
    assert sent[-1][1]['model_profile'] == 'b'
    designer.cancel()
    designer.set_models([], False)
    designer.input.setText('设计')
    designer.submit()
    assert len(sent) == 2 and not designer.send.isEnabled()
    assert '配置模型' in designer.input.placeholderText()
    designer.close()


def test_context_drawers_and_clear_history():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.setStyleSheet(STYLE)
    panel.resize(1000, 700)
    panel.show()
    app.processEvents()
    assert not panel.inspector.isVisible()
    panel.designer_button.click()
    assert panel.designer.isVisible() and not panel.inspector.isVisible()
    panel.inspector_button.click()
    assert panel.inspector.isVisible() and not panel.designer.isVisible()
    panel.inspector_close.click()
    assert not panel.inspector.isVisible()
    designer = panel.designer
    commands = []
    designer.command.connect(lambda action, args: commands.append(action))
    designer.input.setText("draft")
    designer.submit()
    request_id = designer.request_id
    designer.clear_history.click()
    assert commands == ['flow_design', 'flow_design_cancel']
    designer.accept_result({"request_id": request_id, "reply": "late"})
    assert not designer.history and not designer.transcript.toPlainText()
    designer.input.setText('重试')
    designer.submit()
    designer.accept_result({'request_id': designer.request_id, 'error': '<连接失败>'})
    assert '<连接失败>' in designer.transcript.toPlainText()
    assert all('<连接失败>' not in row['content'] for row in designer.history)
    panel.close()


@pytest.mark.parametrize('width', [1100, 500])
def test_working_context(width, tmp_path):
    app = QApplication.instance() or QApplication([])
    panel = WorkingPanel()
    panel.setStyleSheet(STYLE)
    panel.resize(width, 700)
    panel.set_runs([{'run_id': 'r', 'title': 'Flow', 'status': 'running', 'nodes': [
        {'id': 'n', 'title': '节点', 'status': 'running', 'task': '任务',
         'model': '模型', 'tools': [{'name': 'read_file'}], 'subagent_count': 1}]}])
    panel.show()
    app.processEvents()
    item = panel.listing.item(1)
    pos = panel.listing.visualItemRect(item).center()
    QTest.mouseClick(panel.listing.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert not panel.detail.isVisible()
    QTest.keyClick(panel.listing, Qt.Key.Key_Menu)
    assert panel.detail.isVisible() and 'read_file' in panel.details.toPlainText()
    app.processEvents()
    panel.grab().save(str(tmp_path / f'working-{width}.png'))
    panel.close_detail.click()
    assert not panel.detail.isVisible()
    QTest.mouseClick(panel.listing.viewport(), Qt.MouseButton.RightButton, pos=pos)
    assert panel.detail.isVisible()
    panel.close()


@pytest.mark.parametrize('width', [1100, 500])
def test_node_context_and_draft(width, tmp_path):
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.setStyleSheet(STYLE)
    panel.resize(width, 700)
    panel.set_flow({'id': 'f', 'title': 'Flow', 'revision': 1, 'nodes': [], 'edges': []})
    panel.add_node(QPointF(0, 0))
    panel.show()
    app.processEvents()
    assert panel.width() <= width + 5, ('initial', panel.width(), panel.minimumSizeHint())
    node = next(iter(panel.nodes.values()))
    pos = panel.canvas.mapFromScene(node.mapToScene(QPointF(80, 30)))
    QTest.mouseClick(panel.canvas.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert not panel.inspector.isVisible()
    QTest.mouseClick(panel.canvas.viewport(), Qt.MouseButton.RightButton, pos=pos)
    assert panel.inspector.isVisible()
    QTest.qWait(20)
    assert panel.width() <= width + 5, ('inspector', panel.width(), panel.minimumSizeHint())
    assert panel.canvas.viewport().rect().contains(panel.canvas.mapFromScene(node.sceneBoundingRect()).boundingRect())
    panel.prompt_edit.setPlainText('未保存的指令')
    panel.inspector_close.click()
    app.processEvents()
    assert panel.canvas.isVisible() and not panel.inspector.isVisible()
    panel.designer_button.click()
    panel.designer.set_models([], False)
    QTest.qWait(20)
    assert panel.width() <= width + 5, (panel.minimumSizeHint(), panel.designer.minimumSizeHint(), panel.canvas.minimumSizeHint())
    assert panel.canvas.viewport().rect().contains(panel.canvas.mapFromScene(node.sceneBoundingRect()).boundingRect())
    panel.grab().save(str(tmp_path / f'designer-{width}.png'))
    panel.designer.close_button.click()
    QTest.keyClick(panel.canvas, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
    assert panel.inspector.isVisible()
    assert panel.prompt_edit.toPlainText() == '未保存的指令'
    QTest.qWait(20)
    assert panel.inspector_panel.width() >= 230
    assert panel.inspector_panel.geometry().right() < panel.width()
    panel.grab().save(str(tmp_path / f'flow-{width}.png'))
    panel.close()


def test_flow_summary_does_not_invent_child_counts():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.set_flow({'id': 'f', 'title': 'Flow', 'revision': 1, 'nodes': [
        {'id': 'n', 'title': '分析', 'prompt': '分析反馈', 'model_profile': 'runtime-saved'}],
        'edges': [], 'run': {'nodes': {'n': {'status': 'running'}}}},
        [{'id': 'runtime-saved', 'name': '已保存模型'}])
    summary = panel.nodes['n'].toolTip()
    assert '已保存模型' in summary
    assert '运行中' in summary and '分析反馈' in summary
    assert '0 活跃 / 0 总数' not in summary
    assert '子 Agent' not in summary, '普通 Flow 记录没有权威子 Agent 计数'
    panel.close()


def test_unchanged_models_keep_open_selection():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    designer = panel.designer
    models = [{'id': 'one', 'name': '模型一'}, {'id': 'two', 'name': '模型二'}]
    designer.set_models(models, default_model_available=False)
    designer.show()
    designer.model.showPopup()
    app.processEvents()
    designer.model.view().setCurrentIndex(designer.model.model().index(1, 0))
    assert designer.model.view().currentIndex().data(Qt.ItemDataRole.UserRole) == 'two'
    designer.set_models(models, default_model_available=False)
    assert designer.model.view().currentIndex().data(Qt.ItemDataRole.UserRole) == 'two'
    designer.model.hidePopup()
    panel.close()


def test_designer_skips_unconfigured_environment_profile():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.designer.set_models([{'id': 'env', 'name': '未配置预设', 'read_only': True, 'has_api_key': False},
                               {'id': 'local', 'name': '本地连接', 'read_only': False, 'has_api_key': False}],
                              default_model_available=False)
    assert panel.designer.model.currentData() == 'local'
    panel.close()
