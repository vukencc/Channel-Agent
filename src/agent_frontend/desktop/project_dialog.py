"""创建项目时明确选择经典 Agent 或可视化 Flow。"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel,
    QLineEdit, QToolButton, QTextEdit, QVBoxLayout, QWidget,
)

from .theme import ChoiceBox
from .project_picker import ProjectPicker


class ProjectDialog(QDialog):
    def __init__(self, parent=None, *, mode='agent'):
        super().__init__(parent)
        self.setWindowTitle('新建项目')
        self.setMinimumWidth(420)
        self.resize(540, 570)
        self.result_payload = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        title = QLabel('从一个目标开始')
        title.setObjectName('dialogTitle')
        layout.addWidget(title)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setObjectName('muted')
        layout.addWidget(self.description)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(14)
        self.mode = ChoiceBox()
        self.mode.addItem('Agent · 经典对话', 'agent')
        self.mode.addItem('Flow · 节点与交接', 'flow')
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        self.title = QLineEdit('新项目')
        self.title.setMaxLength(120)
        self.project_picker = ProjectPicker(self)
        self.workspace = self.project_picker.path_edit
        self.workspace.setPlaceholderText('已有 Linux 项目目录；可稍后在设置中选择')
        self.environment = ChoiceBox()
        for label, value in [('使用默认环境', None), ('隔离沙箱', 'sandbox'), ('Linux 本机', 'local')]:
            self.environment.addItem(label, value)
        self.policy = ChoiceBox()
        for label, value in [('使用默认权限', None), ('智能申请', 'smart'), ('只读', 'readonly'), ('完全访问', 'full_access')]:
            self.policy.addItem(label, value)
        form.addRow('模式', self.mode)
        form.addRow('名称', self.title)
        form.addRow('项目目录', self.project_picker)
        form.addRow('执行环境', self.environment)
        form.addRow('权限模式', self.policy)
        layout.addLayout(form)
        self.context_toggle = QToolButton()
        self.context_toggle.setText('可选补充背景')
        self.context_toggle.setCheckable(True)
        self.context_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        layout.addWidget(self.context_toggle)
        self.context_area = QWidget()
        context_layout = QVBoxLayout(self.context_area)
        context_layout.setContentsMargins(0, 0, 0, 0)
        self.handoff_label = QLabel('项目背景（可选）')
        self.handoff = QTextEdit()
        self.handoff.setAcceptRichText(False)
        self.handoff.setPlaceholderText('补充项目现状、已有工作或验证方式。')
        self.handoff.setMinimumHeight(80)
        context_layout.addWidget(self.handoff_label)
        context_layout.addWidget(self.handoff)
        layout.addWidget(self.context_area)
        self.context_toggle.toggled.connect(self._context_changed)
        self.handoff.textChanged.connect(self._expand_existing_context)
        self.error = QLabel()
        self.error.setObjectName('error')
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('创建项目')
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName('primary')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        for button in buttons.buttons():
            button.setIcon(QIcon())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)

    def _mode_changed(self, *_):
        flow = self.mode.currentData() == 'flow'
        self.context_toggle.setVisible(flow)
        self._context_changed(self.context_toggle.isChecked())
        self.description.setText('上游完成后会自动把结果和待办交给下游，无需手填。运行前选择已有项目。'
                                 if flow else '使用经典对话完成任务，项目目录可留空以使用默认工作区。')

    def _context_changed(self, checked):
        self.context_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.context_area.setVisible(checked and self.mode.currentData() == 'flow')

    def _expand_existing_context(self):
        if self.handoff.toPlainText():
            self.context_toggle.setChecked(True)

    def accept(self):
        if not self.title.text().strip():
            self.error.setText('请填写项目名称。')
            self.error.show()
            return
        self.result_payload = {
            'mode': self.mode.currentData(), 'title': self.title.text().strip(),
            'workspace_path': self.workspace.text().strip() or None,
            'execution_environment': self.environment.currentData(),
            'permission_policy': self.policy.currentData(),
            'handoff': self.handoff.toPlainText() if self.mode.currentData() == 'flow' else '',
        }
        super().accept()
