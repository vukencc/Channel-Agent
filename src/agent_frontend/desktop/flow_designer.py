"""Flow 设计对话，只发送用户输入的纯文本。"""
import html
import json
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QBoxLayout, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget


class DesignerInput(QTextEdit):
    submit_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setAcceptRichText(False)
        self.setFixedHeight(96)
        self.textChanged.connect(self._limit_text)

    def _limit_text(self):
        text = self.toPlainText()
        if len(text) > 16000:
            cursor = self.textCursor()
            # Qt 的光标位置以 UTF-16 代码单元计数，限制仍按 Unicode 字符计数。
            cursor.setPosition(len(text[:16000].encode('utf-16-le')) // 2)
            cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()

    def text(self):
        return self.toPlainText()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submit_requested.emit()
        else:
            super().keyPressEvent(event)


class FlowDesigner(QWidget):
    command = Signal(str, dict)
    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = []
        self.busy = False
        self.request_id = None
        self.default_model_available = True
        self.retry_available = False
        self.retry_import_request_id = None
        self._pulse = 0
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel('Flow 设计助手'), 1)
        self.close_button = QPushButton('关闭')
        self.close_button.setAccessibleName('关闭设计助手')
        self.close_button.clicked.connect(self.close_requested.emit)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        self.choices = QHBoxLayout()
        self.model = QComboBox()
        self.effort = QComboBox()
        for box in (self.model, self.effort):
            box.setMinimumWidth(0)
            box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.effort.addItem('默认强度', None)
        for value in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
            self.effort.addItem(value, value)
        self.choices.addWidget(self.model)
        self.choices.addWidget(self.effort)
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        layout.addWidget(self.transcript, 1)
        self.status = QLabel('正在设计流程…')
        self.status.setStyleSheet('color: #a8c9ee;')
        layout.addWidget(self.status)
        self.input = DesignerInput()
        self.input.setObjectName('designerInput')
        self.input.setPlaceholderText('描述想要设计的流程')
        self.composer = QFrame()
        self.composer.setObjectName('designerComposer')
        composer_layout = QVBoxLayout(self.composer)
        composer_layout.setContentsMargins(6, 6, 6, 6)
        composer_layout.setSpacing(5)
        composer_layout.addWidget(self.input)
        self.bottom = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.bottom.addLayout(self.choices, 1)
        buttons = QHBoxLayout()
        self.send = QPushButton('发送')
        self.send.setObjectName('designerSend')
        self.stop = QPushButton('停止')
        self.retry = QPushButton('重试')
        self.retry.clicked.connect(self.retry_request)
        buttons.addWidget(self.send)
        buttons.addWidget(self.stop)
        buttons.addWidget(self.retry)
        self.clear_history = QPushButton('清空对话')
        self.clear_history.setAccessibleName('清空设计助手对话')
        self.clear_history.clicked.connect(self.reset_history)
        header.insertWidget(1, self.clear_history)
        self.bottom.addLayout(buttons)
        composer_layout.addLayout(self.bottom)
        layout.addWidget(self.composer)
        self.send.clicked.connect(self.submit)
        self.input.submit_requested.connect(self.submit)
        self.stop.clicked.connect(self.cancel)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate_status)
        self.timer.start(500)
        self.setStyleSheet('''
            FlowDesigner { background: #20262e; color: #e1e8f2; }
            QLabel { color: #e1e8f2; }
            QTextEdit { background: #191f27; border: 1px solid #394554; padding: 8px; color: #e1e8f2; }
            QFrame#designerComposer { background: #191f27; border: 1px solid #465364; }
            QTextEdit#designerInput { background: transparent; border: none; padding: 4px; }
            QFrame#designerComposer QComboBox { padding: 5px 16px 5px 6px; }
            QFrame#designerComposer QComboBox::drop-down { width: 14px; }
            QPushButton { padding: 5px 8px; }
            QPushButton#designerSend { background: #9bbff1; border: none; color: #102038; }
            QPushButton#designerSend:disabled { background: #364657; color: #8595a6; }
        ''')
        self._refresh()

    def _append_message(self, role, text):
        bar = self.transcript.verticalScrollBar()
        following = bar.value() >= bar.maximum() - 4
        position = bar.value()
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.transcript.document().isEmpty():
            cursor.insertBlock()
            cursor.insertBlock()
        color = {'你': '#a8c9ee', '助手': '#d8e5f4', '提示': '#d9bd83', '错误': '#ef9b9b'}[role]
        body = html.escape(text).replace('\n', '<br>')
        label = f'<span style="color:{color}; font-weight:600">{role}</span>'
        if role in ('你', '错误'):
            background = '#293b4a' if role == '你' else '#482e32'
            cursor.insertHtml(f'<table width="100%" cellspacing="0" cellpadding="8" bgcolor="{background}"><tr><td>{label}<br><br>{body}</td></tr></table>')
        else:
            cursor.insertHtml(f'<p style="margin-top:12px; margin-bottom:3px">{label}</p><p style="margin-top:0; margin-bottom:10px">{body}</p>')
        bar.setValue(bar.maximum() if following else position)

    def set_models(self, models, default_model_available=True):
        choices = [(str(row.get('name') or row['id']), row['id']) for row in models
                   if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id']
                   and (not row.get('read_only') or row.get('has_api_key'))]
        catalog = (tuple(choices), bool(default_model_available))
        if catalog == getattr(self, '_model_catalog', None):
            return
        self._model_catalog = catalog
        current = self.model.currentData()
        self.default_model_available = default_model_available
        self.model.blockSignals(True)
        self.model.clear()
        if default_model_available:
            self.model.addItem('默认模型', None)
        for label, identifier in choices:
            self.model.addItem(label, identifier)
        index = self.model.findData(current) if current is not None else -1
        self.model.setCurrentIndex(index if index >= 0 else (0 if self.model.count() else -1))
        self.model.blockSignals(False)
        self.model.setPlaceholderText('请先配置模型')
        self.input.setPlaceholderText('描述想要设计的流程' if self.model.count() else '请先配置模型')
        self._refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        direction = QBoxLayout.Direction.TopToBottom if event.size().width() <= 230 else QBoxLayout.Direction.LeftToRight
        self.bottom.setDirection(direction)
        self.choices.setDirection(direction)
        self.composer.setMinimumHeight(220 if direction == QBoxLayout.Direction.TopToBottom else 150)

    def _refresh(self):
        self.status.setVisible(self.busy)
        self.retry.setVisible(self.retry_available and not self.busy)
        self.retry.setText('重试导入' if self.retry_import_request_id else '重试')
        self.send.setVisible(not self.busy)
        self.stop.setVisible(self.busy)
        self.send.setText('生成中…' if self.busy else '发送')
        self.send.setEnabled(not self.busy and (self.default_model_available or self.model.count() > 0))
        self.send.setToolTip('请先在模型管理中配置连接' if not self.default_model_available and not self.model.count() else '')
        self.stop.setEnabled(self.busy)

    def _animate_status(self):
        if self.busy:
            self._pulse = (self._pulse + 1) % 4
            self.status.setText('正在设计流程' + '·' * (self._pulse + 1))

    def _remember(self, role: str, content: str) -> None:
        self.history = (self.history + [{'role': role, 'content': content}])[-20:]
        while len(self.history) > 1 and sum(len(row['content']) for row in self.history) > 64000:
            self.history.pop(0)

    def submit(self):
        text = self.input.text().strip()[:16000]
        if not text or self.busy or (not self.default_model_available and not self.model.count()):
            return
        self.input.clear()
        self.retry_import_request_id = None
        self._remember('user', text)
        self._append_message('你', text)
        self._start_request()

    def retry_request(self):
        if self.retry_available and not self.busy and self.history:
            self._start_request()

    def _start_request(self):
        self.retry_available = False
        self.busy = True
        self.request_id = uuid.uuid4().hex
        self._refresh()
        payload = {'request_id': self.request_id, 'messages': list(self.history), 'model_profile': self.model.currentData(), 'reasoning_effort': self.effort.currentData()}
        if self.retry_import_request_id:
            payload['retry_import_request_id'] = self.retry_import_request_id
        self.command.emit('flow_design', payload)

    def cancel(self):
        if self.busy or self.retry_import_request_id:
            self.command.emit('flow_design_cancel', {})
        self.busy = False
        self.request_id = None
        self.retry_import_request_id = None
        self.retry_available = False
        self._refresh()

    def reset_history(self):
        self.cancel()
        self.history.clear()
        self.retry_available = False
        self.retry_import_request_id = None
        self.transcript.clear()
        self.input.clear()
        self._refresh()

    def accept_result(self, result):
        if not self.busy or result.get('request_id') != self.request_id:
            return
        self.busy = False
        self.request_id = None
        self._refresh()
        if result.get('error') or result.get('cancelled'):
            error = result.get('error')
            error = error.strip() if isinstance(error, str) else ''
            token = result.get('retry_import_request_id')
            self.retry_import_request_id = token if result.get('error_stage') == 'import' and isinstance(token, str) and token else None
            self._append_message('错误', '设计未完成：' + error if error else '设计未完成。')
            self.retry_available = bool(error)
            self._refresh()
            return
        self.retry_import_request_id = None
        reply = str(result.get('reply') or '')
        template = result.get('template')
        content = reply
        if template is not None:
            content = json.dumps({'reply': reply, 'template': template}, ensure_ascii=False, separators=(',', ':'))
            if len(content) > 16000:
                content = json.dumps({'reply': '', 'template': template}, ensure_ascii=False, separators=(',', ':'))
                if len(content) <= 16000:
                    self._append_message('提示', '回复过长，后续对话只保留完整模板。')
                else:
                    content = reply[:16000]
                    self._append_message('提示', '模板过长，后续对话无法包含完整模板；请缩小流程或重新描述。')
        if content:
            self._remember('assistant', content[:16000])
        self._append_message('助手', reply)
