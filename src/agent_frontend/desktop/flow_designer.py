"""Flow 设计对话，只发送用户输入的纯文本。"""
import json
import uuid

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QBoxLayout, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget


class FlowDesigner(QWidget):
    command = Signal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = []
        self.busy = False
        self.request_id = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Flow 设计助手'))
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
        layout.addLayout(self.choices)
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        layout.addWidget(self.transcript, 1)
        self.input = QLineEdit()
        self.input.setMaxLength(16000)
        self.input.setPlaceholderText('描述想要设计的流程')
        layout.addWidget(self.input)
        buttons = QHBoxLayout()
        self.send = QPushButton('发送')
        self.stop = QPushButton('停止')
        buttons.addWidget(self.send)
        buttons.addWidget(self.stop)
        layout.addLayout(buttons)
        self.send.clicked.connect(self.submit)
        self.input.returnPressed.connect(self.submit)
        self.stop.clicked.connect(self.cancel)
        self._refresh()

    def set_models(self, models):
        current = self.model.currentData()
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItem('默认模型', None)
        for row in models:
            self.model.addItem(str(row.get('name') or row['id']), row['id'])
        self.model.setCurrentIndex(max(0, self.model.findData(current)))
        self.model.blockSignals(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        direction = QBoxLayout.Direction.TopToBottom if event.size().width() <= 230 else QBoxLayout.Direction.LeftToRight
        self.choices.setDirection(direction)

    def _refresh(self):
        self.send.setEnabled(not self.busy)
        self.stop.setEnabled(self.busy)

    def _remember(self, role: str, content: str) -> None:
        self.history = (self.history + [{'role': role, 'content': content}])[-20:]
        while len(self.history) > 1 and sum(len(row['content']) for row in self.history) > 64000:
            self.history.pop(0)

    def submit(self):
        text = self.input.text().strip()
        if not text or self.busy:
            return
        self.input.clear()
        self._remember('user', text)
        self.transcript.insertPlainText('你：' + text + '\n')
        self.busy = True
        self.request_id = uuid.uuid4().hex
        self._refresh()
        self.command.emit('flow_design', {'request_id': self.request_id, 'messages': list(self.history), 'model_profile': self.model.currentData(), 'reasoning_effort': self.effort.currentData()})

    def cancel(self):
        if self.busy:
            self.command.emit('flow_design_cancel', {})
            self.busy = False
            self.request_id = None
            self._refresh()

    def accept_result(self, result):
        if not self.busy or result.get('request_id') != self.request_id:
            return
        self.busy = False
        self.request_id = None
        self._refresh()
        if result.get('error') or result.get('cancelled'):
            self.transcript.insertPlainText('设计未完成。\n')
            return
        reply = str(result.get('reply') or '')
        template = result.get('template')
        content = reply
        if template is not None:
            content = json.dumps({'reply': reply, 'template': template}, ensure_ascii=False, separators=(',', ':'))
            if len(content) > 16000:
                content = json.dumps({'reply': '', 'template': template}, ensure_ascii=False, separators=(',', ':'))
                if len(content) <= 16000:
                    self.transcript.insertPlainText('提示：回复过长，后续对话只保留完整模板。\n')
                else:
                    content = reply[:16000]
                    self.transcript.insertPlainText('提示：模板过长，后续对话无法包含完整模板；请缩小流程或重新描述。\n')
        if content:
            self._remember('assistant', content[:16000])
        self.transcript.insertPlainText('助手：' + reply + '\n')
