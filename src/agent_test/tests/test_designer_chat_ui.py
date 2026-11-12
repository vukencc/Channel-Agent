"""设计助手的显示与滚动行为。"""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QTextEdit
from agent_frontend.desktop.flow_designer import FlowDesigner


def test_designer_uses_multiline_composer():
    app = QApplication.instance() or QApplication([])
    panel = FlowDesigner()
    assert isinstance(panel.input, QTextEdit)
    panel.input.setText("第一行\n第二行")
    assert panel.input.toPlainText() == "第一行\n第二行"


def test_designer_input_limit_counts_non_bmp_characters():
    app = QApplication.instance() or QApplication([])
    panel = FlowDesigner()
    panel.input.setPlainText('🙂' * 16001)
    assert panel.input.toPlainText() == '🙂' * 16000


def test_retry_keeps_draft_and_does_not_duplicate_user_message():
    app = QApplication.instance() or QApplication([])
    panel = FlowDesigner()
    panel.show()
    assert panel.stop.isHidden() and panel.retry.isHidden()
    commands = []
    panel.command.connect(lambda name, payload: commands.append((name, payload)))
    panel.input.setText('设计流程')
    panel.submit()
    assert not panel.stop.isHidden() and panel.send.isHidden()
    panel.input.setText('下一轮草稿')
    panel.accept_result({'request_id': panel.request_id, 'error': '连接失败'})
    assert panel.stop.isHidden() and not panel.retry.isHidden() and not panel.send.isHidden()
    panel.retry_request()
    assert panel.input.toPlainText() == '下一轮草稿'
    assert commands[-1][1]['messages'] == [{'role': 'user', 'content': '设计流程'}]


def test_import_retry_uses_cached_token_and_preserves_draft():
    app = QApplication.instance() or QApplication([])
    panel = FlowDesigner()
    commands = []
    panel.command.connect(lambda name, payload: commands.append((name, payload)))
    panel.input.setText('设计流程')
    panel.submit()
    original = panel.request_id
    panel.input.setText('下一轮草稿')
    panel.accept_result({'request_id': original, 'error': '导入失败', 'error_stage': 'import', 'retry_import_request_id': original})
    assert panel.retry.text() == '重试导入'
    panel.retry_request()
    assert commands[-1][1]['retry_import_request_id'] == original
    assert commands[-1][1]['request_id'] != original
    assert panel.input.toPlainText() == '下一轮草稿'


def test_cancel_and_clear_discard_pending_import_token():
    app = QApplication.instance() or QApplication([])
    panel = FlowDesigner()
    commands = []
    panel.command.connect(lambda name, payload: commands.append((name, payload)))
    for clear in (False, True):
        panel.input.setText('设计流程')
        panel.submit()
        original = panel.request_id
        panel.accept_result({'request_id': original, 'error': '导入失败', 'error_stage': 'import', 'retry_import_request_id': original})
        commands.clear()
        if clear:
            panel.reset_history()
        else:
            panel.cancel()
        assert [name for name, _ in commands] == ['flow_design_cancel']
        assert not panel.retry_available and panel.retry_import_request_id is None


def test_history_preserves_scroll_when_reading_older_messages():
    app = QApplication.instance() or QApplication([])
    panel = FlowDesigner()
    panel.resize(350, 400)
    panel.show()
    for number in range(30):
        panel._append_message('助手', f'消息 {number} ' * 8)
    app.processEvents()
    bar = panel.transcript.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(0)
    panel._append_message('助手', '新消息')
    app.processEvents()
    assert bar.value() == 0


def test_narrow_composer_controls_do_not_overlap():
    app = QApplication.instance() or QApplication([])
    panel = FlowDesigner()
    panel.resize(230, 650)
    panel.show()
    app.processEvents()
    assert panel.input.geometry().bottom() < panel.model.geometry().top()
    assert panel.model.geometry().bottom() < panel.effort.geometry().top()
    assert panel.effort.geometry().bottom() < panel.send.geometry().top()
