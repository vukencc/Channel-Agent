"""原生对话格式化的内容与安全边界；使用受控界面，不调用模型。"""
from html.parser import HTMLParser
import json

import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from agent_frontend.desktop.presentation import render_safe_text
from agent_frontend.desktop.window import Composer, MainWindow, ModelDialog, SafeTextBrowser


@pytest.fixture(scope='module')
def presentation_app():
    yield QApplication.instance() or QApplication([])


def test_safe_formatting_preserves_code_and_prose(presentation_app):
    source = '# 检查结果\n**完成** `x < 2`\n- 保留 <标签>\n- 第二项\n\n```python\nprint("<&>")\n  indent = 2\n```'
    markup = render_safe_text(source)
    assert '<h3' in markup and '<strong>' in markup and '<pre' in markup and '<li>' in markup
    browser = SafeTextBrowser()
    browser.setHtml(markup)
    text = browser.toPlainText()
    for expected in ['检查结果', '完成', 'x < 2', '保留 <标签>', '第二项', 'print("<&>")', '  indent = 2']:
        assert expected in text
    browser.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize('source', [
    '<img src="file:///tmp/private.svg"> ![remote](https://example.invalid/pixel.png)',
    '[打开](file:///tmp/private.txt) <a href="https://example.invalid">外部链接</a>',
    '<style>body{background:url(file:///tmp/private.png)}</style> **正文**',
    '```html\n<img src="https://example.invalid/a">\n```',
])
def test_formatting_cannot_create_links_images_or_resource_attributes(presentation_app, source):
    markup = render_safe_text(source)

    class ResourceAudit(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag not in {'img', 'a', 'style', 'script', 'object', 'iframe', 'svg'}
            for key, value in attrs:
                assert key not in {'src', 'href', 'data'} and not key.startswith('on')
                assert key != 'style' or 'url(' not in (value or '').casefold()

    ResourceAudit().feed(markup)
    browser = SafeTextBrowser()
    browser.setHtml(markup)
    text = browser.toPlainText()
    # 不解释链接/图片语法，HTML 也保留为用户可以阅读的字面量。
    if not source.startswith('```'):
        assert source.replace('**', '') in text
    else:
        assert '<img src="https://example.invalid/a">' in text
    browser.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_streaming_unclosed_code_fence_keeps_every_content_line(presentation_app):
    browser = SafeTextBrowser()
    browser.setHtml(render_safe_text('说明\n```python\nif x < 3:\n    print("未完成")'))
    assert '说明' in browser.toPlainText()
    assert 'if x < 3:\n    print("未完成")' in browser.toPlainText()
    browser.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_model_parameter_editor_never_interprets_json_strings_as_html(presentation_app):
    parameters = {'literal': '<b>保留标签</b> & 未转义文本'}
    dialog = ModelDialog(model={'parameters': parameters})
    assert dialog.parameters.toPlainText() == json.dumps(parameters, ensure_ascii=False, indent=2)
    dialog.accept()
    assert dialog.result_payload['parameters'] == parameters
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_composer_and_parameter_editor_paste_plain_text(presentation_app):
    mime = QMimeData()
    mime.setText('保留 <说明> 与原始文本')
    mime.setHtml('<b>不应解析的富文本内容</b>')
    composer = Composer()
    dialog = ModelDialog()
    for widget in [composer, dialog.parameters]:
        widget.clear()
        widget.insertFromMimeData(mime)
        assert widget.toPlainText() == mime.text()
    composer.deleteLater()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class PresentationBridge(QObject):
    state_changed = Signal(str)
    error = Signal(str)
    finished = Signal(str, str)
    stopped = Signal()
    is_running = False

    def __init__(self):
        super().__init__()
        self.requests = []

    def request(self, action, **payload):
        self.requests.append((action, payload))


def test_welcome_suggestion_only_fills_draft(presentation_app):
    bridge = PresentationBridge()
    window = MainWindow(bridge)
    window.current_session = {'id': 'draft-session', 'busy': False, 'status': 'idle'}
    window.show()
    suggestion = next(button for button in window.findChildren(QPushButton) if button.property('draftText'))
    QTest.mouseClick(suggestion, Qt.MouseButton.LeftButton)
    assert window.composer.toPlainText() == suggestion.property('draftText')
    assert bridge.requests == []
    assert window.send_button.isEnabled()
    window.close()
    window.deleteLater()
    bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
