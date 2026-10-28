"""原生 Qt Widgets 窗口；所有运行时操作由异步桥接器执行。"""
from __future__ import annotations

import html
import json
from pathlib import PurePosixPath
from typing import Any

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeyEvent, QKeySequence, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget,
    QTabWidget, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from .presentation import message_html
from .runtime import RuntimeBridge
from .flow import FlowPanel
from .project_dialog import ProjectDialog
from .theme import ChoiceBox, ElidingLabel, SessionDelegate, STYLE, icon

STATUS_LABELS = {'idle': '就绪', 'queued': '排队中', 'running': '运行中', 'confirming': '待确认',
                 'stopping': '停止中', 'cancelled': '已停止', 'error': '运行失败',
                 'checkpoint': '阶段已保存', 'interrupted': '已中断'}
POLICY_LABELS = {'smart': '智能申请', 'readonly': '只读', 'full_access': '完全访问'}
ENVIRONMENT_LABELS = {'sandbox': '隔离沙箱', 'local': 'Linux 本机'}


class Composer(QTextEdit):
    submit_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.textChanged.connect(self._fit_height)

    def _fit_height(self) -> None:
        height = max(64, min(144, int(self.document().size().height()) + 12))
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submit_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _focus_panel(self, focused: bool) -> None:
        panel = self.parentWidget()
        if panel and panel.objectName() == 'composerPanel':
            panel.setProperty('focused', focused)
            panel.style().unpolish(panel)
            panel.style().polish(panel)
            panel.update()

    def focusInEvent(self, event) -> None:
        self._focus_panel(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._focus_panel(False)
        super().focusOutEvent(event)


class SafeTextBrowser(QTextBrowser):
    """对话和详情永不隐式加载本地或远程资源。"""

    def loadResource(self, resource_type: int, name: Any) -> QByteArray:
        return QByteArray()


def _label(text: str, name: str = '', *, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _dialog_layout(dialog: QDialog, title: str, description: str, *, height: int = 560) -> tuple[QVBoxLayout, QVBoxLayout]:
    """表单滚动而操作固定，较短屏幕仍可保存或取消。"""
    dialog.setMinimumWidth(420)
    dialog.resize(560, 620)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 22, 24, 20)
    layout.setSpacing(16)
    layout.addWidget(_label(title, 'dialogTitle'))
    layout.addWidget(_label(description, 'muted', wrap=True))
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    fields = QVBoxLayout(content)
    fields.setContentsMargins(0, 4, 8, 4)
    fields.setSpacing(18)
    scroll.setWidget(content)
    layout.addWidget(scroll, 1)
    screen = dialog.screen()
    if screen:
        dialog.resize(min(560, screen.availableGeometry().width() - 40), min(height, screen.availableGeometry().height() - 80))
    return layout, fields


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(14)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    return form


def _save_buttons(dialog: QDialog) -> QDialogButtonBox:
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存')
    buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName('primary')
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
    for button in buttons.buttons():
        button.setIcon(QIcon())
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


class ModelDialog(QDialog):
    """凭据只用于当前提交；关闭或提交后立即清空输入控件。"""

    def __init__(self, parent: QWidget | None = None, model: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle('编辑模型连接' if model else '添加模型连接')
        self.result_payload: dict | None = None
        row = model or {}
        layout, fields = _dialog_layout(self, self.windowTitle(), '连接兼容 OpenAI 的模型服务，随后可在会话设置中选择。')
        form = _form()
        self.name = QLineEdit(row.get('name', ''))
        self.name.setPlaceholderText('例如：日常开发')
        self.model = QLineEdit(row.get('model', ''))
        self.model.setPlaceholderText('服务提供的模型 ID')
        self.base_url = QLineEdit(row.get('base_url', ''))
        self.base_url.setPlaceholderText('https://api.example.com/v1')
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText('留空保留原密钥' if model else '该连接的密钥；不继承环境密钥')
        self.capacity = QLineEdit(str(row.get('context_tokens') or ''))
        self.reserve = QLineEdit(str(row['response_reserve_tokens']) if row.get('response_reserve_tokens') is not None else '')
        self.parameters = QTextEdit()
        self.parameters.setAcceptRichText(False)
        self.parameters.setPlainText(json.dumps(row.get('parameters', {}), ensure_ascii=False, indent=2))
        self.parameters.setMinimumHeight(110)
        self.parameters.setMaximumHeight(160)
        for label, field in [('连接名称', self.name), ('模型 ID', self.model), ('服务地址', self.base_url), ('API 密钥', self.api_key)]:
            form.addRow(_label(label), field)
        fields.addLayout(form)
        fields.addWidget(_label('密钥仅保存在本机私有状态目录，界面不会读回。', 'muted', wrap=True))
        advanced_button = QPushButton('高级参数')
        advanced_button.setObjectName('quiet')
        advanced_button.setCheckable(True)
        advanced_button.setIcon(icon('settings'))
        advanced = QWidget()
        advanced_form = _form()
        advanced.setLayout(advanced_form)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        for label, field in [('上下文 token', self.capacity), ('回复预留 token', self.reserve), ('采样参数 JSON', self.parameters)]:
            advanced_form.addRow(_label(label), field)
        advanced_button.toggled.connect(advanced.setVisible)
        advanced_button.setChecked(bool(row.get('context_tokens') or row.get('response_reserve_tokens') or row.get('parameters')))
        advanced.setVisible(advanced_button.isChecked())
        fields.addWidget(advanced_button)
        fields.addWidget(advanced)
        fields.addStretch()
        self.error_label = _label('', 'error', wrap=True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        layout.addWidget(_save_buttons(self))

    def accept(self) -> None:
        try:
            parameters = json.loads(self.parameters.toPlainText() or '{}')
            if not isinstance(parameters, dict):
                raise ValueError
            self.result_payload = {'name': self.name.text(), 'model': self.model.text(),
                                   'base_url': self.base_url.text(), 'api_key': self.api_key.text(),
                                   'context_tokens': int(self.capacity.text()) if self.capacity.text().strip() else None,
                                   'response_reserve_tokens': int(self.reserve.text()) if self.reserve.text().strip() else None,
                                   'parameters': parameters}
        except (ValueError, TypeError):
            self.error_label.setText('容量应为整数，采样参数应为 JSON 对象。')
            self.error_label.show()
            return
        self.api_key.clear()
        super().accept()

    def reject(self) -> None:
        self.api_key.clear()
        self.result_payload = None
        super().reject()


class SessionDialog(QDialog):
    def __init__(self, parent: QWidget, session: dict, models: list[dict], default_model: str):
        super().__init__(parent)
        self.setWindowTitle('会话设置')
        self.result_payload: dict | None = None
        self.session = session
        layout, fields = _dialog_layout(self, '会话设置', '为当前会话选择模型、项目与执行方式。', height=520)
        form = _form()
        self.title = QLineEdit(session['title'])
        self.title.setMaxLength(120)
        self.model = ChoiceBox()
        self.model.addItem(f"环境默认 · {default_model or '未配置模型'}", None)
        for row in models:
            self.model.addItem(f"{row['name']} · {row['model']}", row['id'])
        index = self.model.findData(session.get('model_profile'))
        self.model.setCurrentIndex(max(0, index))
        self.policy = ChoiceBox()
        for label, value in [('智能申请', 'smart'), ('只读', 'readonly'), ('完全访问', 'full_access')]:
            self.policy.addItem(label, value)
        self.policy.setCurrentIndex(max(0, self.policy.findData(session.get('permission_label', session['permission_policy']))))
        self._original_policy_index = self.policy.currentIndex()
        self.environment = ChoiceBox()
        self.environment.addItem('隔离沙箱', 'sandbox')
        self.environment.addItem('Linux 本机', 'local')
        self.environment.setCurrentIndex(max(0, self.environment.findData(session['execution_environment'])))
        self.workspace = QLineEdit(session.get('workspace_path', '') if session.get('workspace_custom') else '')
        self.workspace.setPlaceholderText('留空使用默认工作区；或选择已有项目目录')
        browse = QPushButton('选择目录…')
        browse.setIcon(icon('folder', size=16))
        browse.clicked.connect(self._browse)
        workspace_row = QWidget()
        workspace_layout = QHBoxLayout(workspace_row)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.addWidget(self.workspace)
        workspace_layout.addWidget(browse)
        can_change = session.get('workspace_can_change', False) and not session.get('parent_id')
        workspace_row.setEnabled(can_change)
        self.environment.setEnabled(can_change)
        for label, widget in [('名称', self.title), ('模型', self.model), ('权限', self.policy), ('命令环境', self.environment), ('项目工作区', workspace_row)]:
            form.addRow(_label(label), widget)
        fields.addLayout(form)
        fields.addWidget(_label('Linux 本机命令使用宿主环境。子 Agent 沿用所选项目和命令环境；切换前先结束关联任务。', 'muted', wrap=True))
        fields.addStretch()
        layout.addWidget(_save_buttons(self))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, '选择已有项目目录', self.workspace.text())
        if path:
            self.workspace.setText(path)

    def accept(self) -> None:
        self.result_payload = {'title': self.title.text(), 'model_profile': self.model.currentData(),
                               'permission_policy': self.session['permission_policy'] if self.policy.currentIndex() == self._original_policy_index else self.policy.currentData()}
        if self.environment.isEnabled():
            self.result_payload['execution_environment'] = self.environment.currentData()
            self.result_payload['workspace_path'] = self.workspace.text().strip() or None
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self, bridge: RuntimeBridge):
        super().__init__()
        self.bridge = bridge
        self.state: dict = {'sessions': [], 'models': []}
        self.current_session: dict | None = None
        self.confirmation_dialog: QDialog | None = None
        self._confirmation_key: tuple[str, str] | None = None
        self._drafts: dict[str, str] = {}
        self._scroll_positions: dict[str, int] = {}
        self._rendered_session: str | None = None
        self._rendered_body = ''
        self._pending_send: tuple[str, str] | None = None
        self._closing = False
        self._shutdown_complete = False
        self._older_requested = False
        self._preview_path = '.'
        self._compact_sidebar = False
        self._pending_close_drafts: dict[str, dict] | None = None
        self.project_dialog: ProjectDialog | None = None
        self.setWindowTitle('Agent · 原生桌面')
        self.setWindowIcon(icon('brand', '#a5c4f4', 32))
        self.resize(1240, 840)
        self.setMinimumSize(520, 460)
        self.setStyleSheet(STYLE)
        self._build()
        self.bridge.state_changed.connect(self._receive_state)
        self.bridge.error.connect(self._show_error)
        self.bridge.finished.connect(self._finished)
        self.bridge.stopped.connect(self._stopped)
        for sequence, callback in [('Ctrl+N', self._new_session), ('Ctrl+L', self.composer.setFocus),
                                   ('Ctrl+B', self._toggle_sidebar), ('Ctrl+Return', self._send), ('Escape', self._cancel)]:
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(callback)
            self.addAction(action)

    def _build(self) -> None:
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        self.sidebar = QWidget()
        self.sidebar.setObjectName('sidebar')
        self.sidebar.setMinimumWidth(208)
        self.sidebar.setMaximumWidth(350)
        left = QVBoxLayout(self.sidebar)
        left.setContentsMargins(14, 22, 14, 14)
        left.setSpacing(12)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(7, 0, 0, 10)
        mark = QLabel()
        mark.setPixmap(icon('brand', '#9bbfee', 26).pixmap(26, 26))
        brand_row.addWidget(mark)
        brand_row.addWidget(_label('Agent', 'brand'))
        brand_row.addStretch()
        brand_row.addWidget(_label('DESKTOP', 'eyebrow'))
        left.addLayout(brand_row)
        self.new_button = QPushButton('新建项目')
        self.new_button.setObjectName('newSession')
        self.new_button.setIcon(icon('plus', '#b4d4ff', 18))
        self.new_button.setToolTip('选择 Agent / Flow 模式 · Ctrl+N')
        self.new_button.clicked.connect(self._new_session)
        left.addWidget(self.new_button)
        self.search = QLineEdit()
        self.search.setObjectName('search')
        self.search.setPlaceholderText('搜索会话')
        self.search.addAction(icon('search', '#74859c', 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_sessions)
        left.addWidget(self.search)
        left.addWidget(_label('会话', 'section'))
        self.sessions = QListWidget()
        self.sessions.setItemDelegate(SessionDelegate(self.sessions))
        self.sessions.setMouseTracking(True)
        self.sessions.setSpacing(0)
        self.sessions.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sessions.itemClicked.connect(self._select_item)
        left.addWidget(self.sessions, 1)
        flow_heading = QHBoxLayout()
        flow_heading.addWidget(_label('Agent Flows', 'section'))
        flow_heading.addStretch()
        self.new_flow_button = QPushButton('创建')
        self.new_flow_button.setObjectName('quiet')
        self.new_flow_button.setIcon(icon('plus', size=15))
        self.new_flow_button.clicked.connect(lambda: self._new_project('flow'))
        flow_heading.addWidget(self.new_flow_button)
        left.addLayout(flow_heading)
        self.flows = QListWidget()
        self.flows.setMaximumHeight(230)
        self.flows.setMinimumHeight(70)
        self.flows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.flows.itemClicked.connect(lambda item: self.bridge.request('flow_select', flow_id=item.data(Qt.ItemDataRole.UserRole)))
        left.addWidget(self.flows, 1)
        models = QPushButton('模型管理')
        models.setObjectName('quiet')
        models.setIcon(icon('model', '#8c9fb9', 18))
        models.clicked.connect(self._models)
        left.addWidget(models)
        self.delete_button = QPushButton('删除当前会话')
        self.delete_button.setObjectName('quiet')
        self.delete_button.setIcon(icon('trash', '#8090a7', 18))
        self.delete_button.clicked.connect(self._delete)
        left.addWidget(self.delete_button)
        self.splitter.addWidget(self.sidebar)
        body = QWidget()
        body.setObjectName('conversation')
        body.setMinimumWidth(260)
        center = QVBoxLayout(body)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(0)
        header = QWidget()
        header.setObjectName('header')
        toolbar = QHBoxLayout(header)
        toolbar.setContentsMargins(18, 16, 20, 15)
        toolbar.setSpacing(10)
        toolbar.addWidget(self._icon_button('menu', '显示 / 隐藏会话栏 · Ctrl+B', self._toggle_sidebar))
        heading = QVBoxLayout()
        heading.setSpacing(5)
        self.title_label = ElidingLabel('正在打开会话…')
        self.title_label.setObjectName('title')
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        heading.addWidget(self.title_label)
        self.workspace_label = ElidingLabel('正在连接…')
        self.workspace_label.setObjectName('muted')
        self.workspace_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        heading.addWidget(self.workspace_label)
        toolbar.addLayout(heading, 1)
        self.status_chip = _label('就绪', 'statusChip')
        self.status_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_chip.setFixedHeight(24)
        toolbar.addWidget(self.status_chip)
        self.settings_button = self._icon_button('settings', '会话设置', self._settings)
        toolbar.addWidget(self.settings_button)
        self.activity_button = self._icon_button('activity', '活动详情', self._toggle_activity)
        self.files_button = self._icon_button('folder', '项目文件', self._files)
        toolbar.addWidget(self.activity_button)
        toolbar.addWidget(self.files_button)
        center.addWidget(header)
        self.workspace_stack = QStackedWidget()
        center.addWidget(self.workspace_stack, 1)
        conversation = QWidget()
        self.agent_page = conversation
        self.conversation_layout = QVBoxLayout(conversation)
        self.conversation_layout.setContentsMargins(28, 14, 28, 18)
        self.conversation_layout.setSpacing(10)
        self.workspace_stack.addWidget(conversation)
        self.flow_page = QWidget()
        flow_layout = QVBoxLayout(self.flow_page)
        flow_layout.setContentsMargins(8, 0, 8, 0)
        self.flow_error = _label('', 'error', wrap=True)
        self.flow_error.hide()
        flow_layout.addWidget(self.flow_error)
        self.flow_panel = FlowPanel()
        self.flow_panel.command.connect(lambda action, args: self.bridge.request(action, **args))
        flow_layout.addWidget(self.flow_panel, 1)
        self.workspace_stack.addWidget(self.flow_page)
        self.error_label = QLabel()
        self.error_label.setObjectName('error')
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.conversation_layout.addWidget(self.error_label)
        history_row = QHBoxLayout()
        self.older_button = QPushButton('加载更早消息')
        self.older_button.setObjectName('history')
        self.older_button.clicked.connect(self._older)
        self.older_button.hide()
        history_row.addWidget(self.older_button)
        history_row.addStretch()
        self.latest_button = QPushButton('回到最新')
        self.latest_button.setObjectName('history')
        self.latest_button.clicked.connect(self._latest)
        self.latest_button.hide()
        history_row.addWidget(self.latest_button)
        self.conversation_layout.addLayout(history_row)
        self.conversation_stack = QStackedWidget()
        self.transcript = SafeTextBrowser()
        self.transcript.document().setDocumentMargin(8)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setOpenLinks(False)
        self.transcript.verticalScrollBar().valueChanged.connect(self._update_latest)
        self.transcript.verticalScrollBar().rangeChanged.connect(self._update_latest)
        self.transcript.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.conversation_stack.addWidget(self.transcript)
        self.welcome = self._welcome()
        self.conversation_stack.addWidget(self.welcome)
        self.conversation_stack.setCurrentWidget(self.welcome)
        self.conversation_layout.addWidget(self.conversation_stack, 1)
        phase_row = QHBoxLayout()
        phase_row.setContentsMargins(4, 0, 0, 0)
        self.phase_mark = QLabel()
        self.phase_mark.setPixmap(icon('activity', '#6e9ab9', 15).pixmap(15, 15))
        phase_row.addWidget(self.phase_mark)
        self.phase_label = QLabel('准备就绪后，即可开始任务')
        self.phase_label.setObjectName('muted')
        self.phase_label.setWordWrap(True)
        phase_row.addWidget(self.phase_label, 1)
        self.conversation_layout.addLayout(phase_row)
        composer_panel = QWidget()
        composer_panel.setObjectName('composerPanel')
        compose = QVBoxLayout(composer_panel)
        compose.setContentsMargins(14, 12, 12, 11)
        compose.setSpacing(8)
        self.composer = Composer()
        self.composer.setObjectName('composer')
        self.composer.setPlaceholderText('描述任务，和 Agent 一起完成…')
        self.composer.setFixedHeight(64)
        self.composer.submit_requested.connect(self._send)
        self.composer.textChanged.connect(self._update_buttons)
        compose.addWidget(self.composer)
        compose_row = QHBoxLayout()
        compose_row.setSpacing(9)
        model_mark = QLabel()
        model_mark.setPixmap(icon('model', '#8199ba', 16).pixmap(16, 16))
        compose_row.addWidget(model_mark)
        self.model_label = ElidingLabel('配置模型后即可开始')
        self.model_label.setObjectName('muted')
        self.model_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        compose_row.addWidget(self.model_label, 1)
        self.stop_button = QPushButton('停止')
        self.stop_button.setObjectName('stop')
        self.stop_button.setIcon(icon('stop', '#ecb6c1', 15))
        self.stop_button.setToolTip('停止当前任务 · Esc')
        self.stop_button.clicked.connect(self._cancel)
        compose_row.addWidget(self.stop_button)
        self.send_button = QPushButton('发送')
        self.send_button.setObjectName('primary')
        self.send_button.setIcon(icon('send', '#253b59', 17))
        self.send_button.setToolTip('Enter 发送 · Shift+Enter 换行')
        self.send_button.clicked.connect(self._send)
        compose_row.addWidget(self.send_button)
        compose.addLayout(compose_row)
        self.conversation_layout.addWidget(composer_panel)
        shortcuts = _label('Enter 发送     Shift+Enter 换行', 'muted')
        shortcuts.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.conversation_layout.addWidget(shortcuts)
        self.splitter.addWidget(body)
        self.splitter.setSizes([252, 988])
        self.splitter.setStretchFactor(1, 1)
        self.activity_dock = QDockWidget('会话详情', self)
        self.activity_dock.setMinimumWidth(225)
        self.activity_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        activity = QWidget()
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(16, 17, 16, 14)
        activity_layout.addWidget(_label('当前任务', 'section'))
        self.activity_text = SafeTextBrowser()
        self.activity_text.setOpenExternalLinks(False)
        self.activity_text.setOpenLinks(False)
        activity_layout.addWidget(self.activity_text)
        self.details = QCheckBox('在对话中显示工具与推理详情')
        self.details.toggled.connect(self._refresh_transcript)
        activity_layout.addWidget(self.details)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(activity, icon('activity', size=16), '活动')
        files = QWidget()
        file_layout = QVBoxLayout(files)
        file_layout.setContentsMargins(14, 17, 14, 14)
        file_layout.setSpacing(12)
        file_toolbar = QHBoxLayout()
        up = QPushButton('上一级')
        up.setIcon(icon('up', size=16))
        up.clicked.connect(self._files_up)
        file_toolbar.addWidget(up)
        refresh = self._icon_button('refresh', '刷新项目文件', lambda: self._files(self._preview_path))
        file_toolbar.addWidget(refresh)
        file_layout.addLayout(file_toolbar)
        self.file_path_label = QLabel('当前项目工作区')
        self.file_path_label.setWordWrap(True)
        self.file_path_label.setObjectName('muted')
        file_layout.addWidget(self.file_path_label)
        file_splitter = QSplitter(Qt.Orientation.Vertical)
        self.file_list = QListWidget()
        self.file_list.setIconSize(QSize(17, 17))
        self.file_list.itemClicked.connect(self._preview_item)
        file_splitter.addWidget(self.file_list)
        self.file_preview = SafeTextBrowser()
        self.file_preview.setOpenLinks(False)
        self.file_preview.setOpenExternalLinks(False)
        file_splitter.addWidget(self.file_preview)
        file_splitter.setSizes([200, 360])
        file_layout.addWidget(file_splitter)
        note = QLabel('只读 · 最多 500 项 / 文本 256 KiB · 不跟随链接')
        note.setObjectName('muted')
        note.setWordWrap(True)
        file_layout.addWidget(note)
        self.detail_tabs.addTab(files, icon('folder', size=16), '文件')
        self.activity_dock.setWidget(self.detail_tabs)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.activity_dock)
        self.activity_dock.hide()
        self.statusBar().showMessage('本机会话与项目 · 正在初始化')
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self._update_buttons()

    @staticmethod
    def _icon_button(name: str, tooltip: str, callback) -> QPushButton:
        button = QPushButton()
        button.setObjectName('iconButton')
        button.setIcon(icon(name, size=19))
        button.setIconSize(QSize(19, 19))
        button.setFixedSize(33, 33)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.clicked.connect(callback)
        return button

    def _welcome(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName('welcome')
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(4, 20, 4, 22)
        outer.addStretch(1)
        panel = QWidget()
        panel.setMaximumWidth(470)
        welcome = QVBoxLayout(panel)
        welcome.setContentsMargins(0, 0, 0, 0)
        welcome.setSpacing(13)
        mark = QLabel()
        mark.setPixmap(icon('brand', '#92b5e3', 34).pixmap(34, 34))
        welcome.addWidget(mark)
        welcome.addWidget(_label('下一步，想完成什么？', 'welcomeTitle', wrap=True))
        welcome.addWidget(_label('描述目标、相关项目和期望的结果。', 'welcomeSubtitle', wrap=True))
        welcome.addSpacing(8)
        for symbol, label, draft in [
            ('folder', '了解项目\n梳理结构，找到关键入口', '请先梳理当前项目的代码结构，说明关键入口、主要模块和运行方式。'),
            ('search', '排查问题\n从复现和证据开始定位', '我想排查一个问题。请先和我确认现象与复现步骤，再检查相关代码并提出修复方案。'),
            ('code', '实现功能\n明确目标，逐步完成并验证', '我想实现一个新功能。请先和我确认目标、使用场景和验收条件，再开始修改。'),
        ]:
            button = QPushButton(label)
            button.setObjectName('suggestion')
            button.setIcon(icon(symbol, '#90aacb', 22))
            button.setIconSize(QSize(22, 22))
            button.setMinimumHeight(64)
            button.setProperty('draftText', draft)
            button.setToolTip('填入草稿，可修改后发送')
            button.clicked.connect(lambda checked=False, text=draft: self._draft_task(text))
            welcome.addWidget(button)
        self.welcome_note = _label('从左侧“模型管理”添加连接，即可开始。', 'muted', wrap=True)
        welcome.addWidget(self.welcome_note)
        panel_row = QHBoxLayout()
        panel_row.addStretch(1)
        panel_row.addWidget(panel, 100)
        panel_row.addStretch(1)
        outer.addLayout(panel_row)
        outer.addStretch(2)
        scroll.setWidget(content)
        return scroll

    def _draft_task(self, text: str) -> None:
        if self.current_session and not self._closing:
            self.composer.setPlainText(text)
            self.composer.setFocus()

    def _receive_state(self, payload: str) -> None:
        if self._closing:
            return
        state = json.loads(payload)
        old = self.current_session
        selected = state.get('selected')
        old_id = old['id'] if old else None
        new_id = selected['id'] if selected else None
        if old_id != new_id:
            if old_id:
                self._drafts[old_id] = self.composer.toPlainText()
                self._scroll_positions[old_id] = self.transcript.verticalScrollBar().value()
            self.composer.setPlainText(self._drafts.get(new_id, ''))
            self._preview_path = '.'
            self.file_list.clear()
            self.file_preview.clear()
        self.state = state
        self.current_session = selected
        self._render_sessions()
        self._render_flows()
        current_flow = state.get('selected_flow')
        self.flow_panel.set_flow(current_flow, state.get('models', []))
        self.workspace_stack.setCurrentWidget(self.flow_page if current_flow else self.agent_page)
        if selected:
            self.title_label.setText(selected['title'])
            self.title_label.setToolTip(html.escape(selected['title']))
            path = selected.get('workspace_path', '')
            self.workspace_label.setText(path or '默认项目工作区')
            self.workspace_label.setToolTip(html.escape(path))
            profile = next((row['name'] for row in state['models'] if row['id'] == selected.get('model_profile')), state.get('default_model') or '未配置模型')
            self.model_label.setText(f"{profile}  ·  {POLICY_LABELS.get(selected['permission_label'], selected['permission_label'])}  ·  {ENVIRONMENT_LABELS.get(selected['execution_environment'], selected['execution_environment'])}")
            self.model_label.setToolTip('点击左侧“模型管理”添加连接，再在会话设置中选择。' if profile == '未配置模型' else html.escape(self.model_label.text()))
            self.welcome_note.setVisible(profile == '未配置模型')
            phase = selected['phase'] or ('就绪' if not selected['busy'] else '正在运行')
            self.phase_label.setText(selected.get('error') or selected.get('workspace_error') or phase)
            self.status_chip.setText(STATUS_LABELS.get(selected['status'], selected['status']))
            tone = 'running' if selected['busy'] else ''
            if selected['status'] in {'confirming', 'queued', 'stopping'}:
                tone = 'warning'
            elif selected.get('error') or selected['status'] in {'error', 'interrupted'}:
                tone = 'error'
            if self.status_chip.property('tone') != tone:
                self.status_chip.setProperty('tone', tone)
                self.status_chip.style().unpolish(self.status_chip)
                self.status_chip.style().polish(self.status_chip)
            self.older_button.setVisible(bool(selected.get('next_before')))
            self._refresh_transcript()
        else:
            self.title_label.setText('开始一个新会话')
            self.workspace_label.setText('点击“新建会话”开始')
            self.transcript.clear()
            self.conversation_stack.setCurrentWidget(self.welcome)
            self.phase_label.setText('暂无会话')
            self.status_chip.setText('未选择')
            self.older_button.hide()
        if current_flow:
            self.title_label.setText(current_flow['title'])
            self.title_label.setToolTip(html.escape(current_flow['title']))
            project_path = self.flow_panel.draft().get('workspace_path')
            self.workspace_label.setText(f'流程项目：{project_path}' if project_path
                                         else '在下方选择项目，也可为起点节点单独设置')
            self.workspace_label.setToolTip(html.escape(self.workspace_label.text()))
            self.activity_dock.hide()
            flow_status = (current_flow.get('run') or {}).get('status', 'idle')
            self.status_chip.setText({'completed': '已完成', 'failed': '失败', 'cancelling': '停止中',
                                      'running': '运行中', 'queued': '排队中', 'cancelled': '已停止'}.get(flow_status, 'Flow'))
        runtime = state['runtime']
        self.statusBar().showMessage(f"{len(state['sessions'])} 个 Agent  ·  {runtime['active_agents']} 个任务运行  ·  {runtime['active_model_calls']} 个模型请求  ·  {len(runtime['active_tools'])} 个工具执行")
        if state.get('load_errors'):
            self._show_error(f"{state['load_errors']} 份会话记录无法读取；原文件已保留。")
        self._render_activity()
        self._update_buttons()
        self._show_confirmation()

    def _render_flows(self) -> None:
        current = (self.state.get('selected_flow') or {}).get('id')
        rows = [(row['id'], row['title'], row['status'], row['node_count']) for row in self.state.get('flows', [])]
        if (rows, current) == getattr(self, '_flow_rows', None):
            return
        self._flow_rows = rows, current
        position = self.flows.verticalScrollBar().value()
        self.flows.clear()
        for identifier, title, status, count in rows:
            label = {'idle': '未运行', 'completed': '已完成', 'failed': '运行失败', 'running': '运行中',
                     'queued': '排队中', 'cancelled': '已停止', 'cancelling': '停止中', 'interrupted': '已中断'}.get(status, status)
            item = QListWidgetItem(f'{title}\n{count} 个节点 · {label}')
            item.setSizeHint(QSize(0, 57))
            item.setData(Qt.ItemDataRole.UserRole, identifier)
            item.setIcon(icon('code', '#91b4dc', 18))
            self.flows.addItem(item)
            if identifier == current:
                self.flows.setCurrentItem(item)
        self.flows.verticalScrollBar().setValue(position)

    def _render_sessions(self) -> None:
        # 仅列表内容变化才重建，状态流不打断用户的列表滚动。
        rows = [(row['id'], row['title'], row['status'], row.get('parent_id')) for row in self.state['sessions']]
        current_id = self.current_session['id'] if self.current_session else None
        if rows == getattr(self, '_session_rows', None) and current_id == getattr(self, '_listed_current', None):
            return
        self._session_rows = rows
        self._listed_current = current_id
        scroll = self.sessions.verticalScrollBar().value()
        self.sessions.clear()
        parents = {identifier: parent for identifier, _, _, parent in rows}
        for identifier, title, status, parent in rows:
            label = f"{title}\n{STATUS_LABELS.get(status, status)}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, identifier)
            depth, ancestor, visited = 0, parent, {identifier}
            while ancestor and ancestor not in visited:
                visited.add(ancestor)
                depth += 1
                ancestor = parents.get(ancestor)
            item.setData(Qt.ItemDataRole.UserRole + 1, {'title': title, 'status': status,
                                                      'status_label': STATUS_LABELS.get(status, status), 'depth': depth})
            item.setToolTip(html.escape(f'{title}\n{identifier}'))
            self.sessions.addItem(item)
            if self.current_session and identifier == self.current_session['id']:
                self.sessions.setCurrentItem(item)
        self.sessions.verticalScrollBar().setValue(scroll)
        self._filter_sessions()

    def _filter_sessions(self) -> None:
        query = self.search.text().casefold()
        for index in range(self.sessions.count()):
            item = self.sessions.item(index)
            item.setHidden(query not in item.text().casefold() and query not in item.data(Qt.ItemDataRole.UserRole))

    def _select_item(self, item: QListWidgetItem) -> None:
        self.bridge.request('select', session_id=item.data(Qt.ItemDataRole.UserRole))

    def render_transcript(self, messages: list[dict], partial: str, *, session_id: str) -> None:
        sections = []
        show_details = self.details.isChecked()
        for message in messages:
            role = message.get('role', '')
            if role == 'system' or role == 'tool' and not show_details:
                continue
            content = message.get('content') or ''
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            name = {'user': '你', 'assistant': 'Agent', 'tool': '工具结果'}.get(role, role)
            if content:
                sections.append(self._message_html(name, content, kind=role))
            if show_details:
                if message.get('reasoning_content'):
                    sections.append(self._message_html('推理详情', message['reasoning_content'], kind='detail'))
                for call in message.get('tool_calls', []):
                    function = call.get('function', {})
                    sections.append(self._message_html(function.get('name', '工具调用'), function.get('arguments', ''), kind='detail'))
        if partial:
            sections.append(self._message_html('Agent · 正在生成', partial))
        self.conversation_stack.setCurrentWidget(self.transcript if sections else self.welcome)
        body = '<html><body style="font-family:sans-serif;color:#d8e1ed;font-size:15px">' + ''.join(sections) + '</body></html>'
        if body == self._rendered_body and session_id == self._rendered_session:
            return
        bar = self.transcript.verticalScrollBar()
        old_value, old_max = bar.value(), bar.maximum()
        same = session_id == self._rendered_session
        following = same and old_max - old_value < 16
        self.transcript.setHtml(body)
        if same:
            target = old_value + max(0, bar.maximum() - old_max) if self._older_requested else bar.maximum() if following else old_value
        else:
            target = self._scroll_positions.get(session_id, bar.maximum())
        bar.setValue(target)
        self._update_latest()
        self._older_requested = False
        self._rendered_session, self._rendered_body = session_id, body

    @staticmethod
    def _message_html(name: str, content: str, *, kind: str = 'assistant') -> str:
        return message_html(name, content, kind=kind)

    def _refresh_transcript(self) -> None:
        if self.current_session:
            self.render_transcript(self.current_session.get('messages', []), self.current_session.get('partial', ''), session_id=self.current_session['id'])

    def _render_activity(self) -> None:
        if not self.current_session:
            self.activity_text.setPlainText('暂无运行活动')
            return
        root = self.current_session['parent_id'] or self.current_session['id']
        rows = [row for row in self.state['sessions'] if row['id'] == root or row.get('parent_id') == root]
        sections = []
        for row in rows:
            name = f"{row['title']} · {STATUS_LABELS.get(row['status'], row['status'])}"
            sections.append(message_html(name, row['phase'] or '等待下一步任务', kind='detail'))
        ids = {row['id'] for row in rows}
        tools = [row for row in self.state['runtime']['active_tools'] if row.get('session_id') in ids]
        if tools:
            for row in tools:
                sections.append(message_html('执行中的工具', f"{row.get('name', row.get('tool', '工具'))} · {row.get('state', row.get('status', 'running'))}", kind='detail'))
        self.activity_text.setHtml('<html><body style="color:#c6d3e4;font-size:12px">' + ''.join(sections) + '</body></html>')

    def _update_buttons(self) -> None:
        session = self.current_session
        idle = bool(session and not session['busy'] and not session.get('tool_workers'))
        active = bool(session and session['busy'])
        self.send_button.setEnabled(bool(idle and self.composer.toPlainText().strip() and not self._pending_send and not self._closing))
        self.stop_button.setEnabled(active and session['status'] != 'stopping' and not self._closing)
        self.stop_button.setVisible(active)
        self.settings_button.setEnabled(idle and not self._closing)
        self.delete_button.setEnabled(idle and not session.get('confirmation') and not self._closing)
        self.new_button.setEnabled(not self._closing)
        self.new_flow_button.setEnabled(not self._closing)
        current_flow = self.state.get('selected_flow') if self.state else None
        if current_flow:
            running = (current_flow.get('run') or {}).get('status') in {'queued', 'running', 'cancelling'}
            self.settings_button.setEnabled(False)
            self.delete_button.setText('删除当前 Flow')
            self.delete_button.setEnabled(not running and not self._closing)
        else:
            self.delete_button.setText('删除当前会话')
        self.activity_button.setEnabled(bool(session) and not current_flow)
        self.files_button.setEnabled(bool(session) and not current_flow)

    def _send(self) -> None:
        if not self.send_button.isEnabled() or not self.current_session:
            return
        text = self.composer.toPlainText()
        self._pending_send = (self.current_session['id'], text)
        self.error_label.hide()
        self.bridge.request('send', session_id=self.current_session['id'], text=text)
        self._update_buttons()

    def _finished(self, action: str, payload: str) -> None:
        result = json.loads(payload)
        if action.startswith('flow_'):
            self.flow_panel.accept_result(action, result)
            if not result.get('error'):
                self.flow_error.hide()
            if action == 'flow_save' and self._pending_close_drafts is not None:
                if result.get('error'):
                    self._pending_close_drafts = None
                    self.centralWidget().setEnabled(True)
                else:
                    self._pending_close_drafts.pop(result['id'], None)
                    if not self._pending_close_drafts:
                        self._pending_close_drafts = None
                        self._begin_shutdown()
        if action in {'files', 'file'} and not result.get('error') and self.current_session and result.get('session_id') == self.current_session['id']:
            self.file_path_label.setText(result['path'] + ('（仅前 256 KiB）' if result.get('truncated') else ''))
            if action == 'files':
                self._preview_path = result['path']
                self.file_list.clear()
                for row in result['entries']:
                    item = QListWidgetItem(row['name'])
                    item.setIcon(icon('folder' if row['is_dir'] else 'file', '#91b4dc' if row['is_dir'] else '#8b9db5', 17))
                    item.setData(Qt.ItemDataRole.UserRole, row)
                    self.file_list.addItem(item)
            else:
                self.file_preview.setPlainText(result['content'])
        if action == 'send' and self._pending_send:
            identifier, draft = self._pending_send
            if not result.get('error'):
                self._drafts.pop(identifier, None)
                if self.current_session and self.current_session['id'] == identifier and self.composer.toPlainText() == draft:
                    self.composer.clear()
            self._pending_send = None
            self._update_buttons()

    def _cancel(self) -> None:
        flow = self.state.get('selected_flow') if self.state else None
        if flow:
            self.bridge.request('flow_cancel', flow_id=flow['id'])
            return
        if self.current_session and self.current_session['busy']:
            self.bridge.request('cancel', session_id=self.current_session['id'])

    def _new_session(self) -> None:
        self._new_project('agent')

    def _new_project(self, mode: str = 'agent') -> None:
        if self._closing:
            return
        dialog = ProjectDialog(self, mode=mode)
        self.project_dialog = dialog
        if dialog.exec() == QDialog.DialogCode.Accepted:
            payload = dict(dialog.result_payload)
            flow = payload.pop('mode') == 'flow'
            if not flow:
                payload.pop('handoff', None)
            self.bridge.request('flow_create' if flow else 'create', **payload)
        self.project_dialog = None
        dialog.deleteLater()

    def _older(self) -> None:
        if self.current_session:
            self._older_requested = True
            self.bridge.request('older', session_id=self.current_session['id'])

    def _latest(self) -> None:
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _update_latest(self, *_: Any) -> None:
        bar = self.transcript.verticalScrollBar()
        self.latest_button.setVisible(bar.maximum() > 0 and bar.maximum() - bar.value() >= 16)

    def _toggle_sidebar(self) -> None:
        self.sidebar.setVisible(not self.sidebar.isVisible())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not hasattr(self, 'sidebar'):
            return
        compact = self.width() < 760
        if compact and not self._compact_sidebar:
            self.sidebar.hide()
        self._compact_sidebar = compact
        self.status_chip.setVisible(not compact)
        margin = 14 if compact else 28
        self.conversation_layout.setContentsMargins(margin, 12, margin, 14 if compact else 18)

    def _toggle_activity(self) -> None:
        if self.width() < 920:
            self.sidebar.hide()
        self.activity_dock.setVisible(not self.activity_dock.isVisible())
        self.detail_tabs.setCurrentIndex(0)

    def _files(self, path: str = '.') -> None:
        if not self.current_session:
            return
        if not isinstance(path, str):
            path = '.'
        if self.width() < 920:
            self.sidebar.hide()
        self.activity_dock.show()
        self.detail_tabs.setCurrentIndex(1)
        self.bridge.request('files', session_id=self.current_session['id'], path=path)

    def _files_up(self) -> None:
        self._files(str(PurePosixPath(self._preview_path).parent))

    def _preview_item(self, item: QListWidgetItem) -> None:
        if not self.current_session:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        if row['is_dir']:
            self._files(row['path'])
        else:
            self.bridge.request('file', session_id=self.current_session['id'], path=row['path'])

    def _settings(self) -> None:
        if not self.current_session or not self.settings_button.isEnabled():
            return
        identifier = self.current_session['id']
        dialog = SessionDialog(self, self.current_session, self.state['models'], self.state.get('default_model', '默认'))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.bridge.request('settings', session_id=identifier, **dialog.result_payload)

    def _models(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle('模型管理')
        dialog.resize(600, 490)
        dialog.setMinimumSize(420, 340)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        layout.addWidget(_label('模型连接', 'dialogTitle'))
        layout.addWidget(_label('添加兼容 OpenAI API 的服务，在会话设置中选择使用。', 'muted', wrap=True))
        stack = QStackedWidget()
        listing = QListWidget()
        listing.setIconSize(QSize(24, 24))
        stack.addWidget(listing)
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(24, 24, 24, 24)
        empty_layout.addStretch()
        mark = QLabel()
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(icon('model', '#87a9d5', 38).pixmap(38, 38))
        empty_layout.addWidget(mark)
        title = _label('添加第一个模型连接', 'title', wrap=True)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(title)
        note = _label('准备好模型 ID、服务地址和密钥，\n就可以开始第一个任务。', 'muted', wrap=True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(note)
        empty_layout.addStretch()
        stack.addWidget(empty)
        layout.addWidget(stack, 1)

        def refresh(*_: Any) -> None:
            current = listing.currentItem().data(Qt.ItemDataRole.UserRole) if listing.currentItem() else None
            listing.clear()
            for row in self.state.get('models', []):
                item = QListWidgetItem(f"{row['name']}\n{row['model']}" + ('  ·  环境配置，只读' if row['read_only'] else ''))
                item.setIcon(icon('model', '#8badde', 24))
                item.setSizeHint(QSize(0, 68))
                item.setData(Qt.ItemDataRole.UserRole, row['id'])
                item.setToolTip(html.escape(f"{row['name']}\n{row['model']}\n{row.get('base_url', '')}"))
                listing.addItem(item)
                if row['id'] == current:
                    listing.setCurrentItem(item)
            if listing.count() and listing.currentItem() is None:
                listing.setCurrentRow(0)
            stack.setCurrentWidget(listing if listing.count() else empty)
            update_actions()

        def selected() -> dict | None:
            item = listing.currentItem()
            return next((row for row in self.state['models'] if item and row['id'] == item.data(Qt.ItemDataRole.UserRole)), None)

        def edit(create: bool = False) -> None:
            row = None if create else selected()
            if not create and (not row or row['read_only']):
                return
            editor = ModelDialog(dialog, row)
            if editor.exec() == QDialog.DialogCode.Accepted:
                payload = editor.result_payload
                editor.result_payload = None
                self.bridge.request('model_add' if create else 'model_edit', **({'model_id': row['id']} if row else {}), **payload)
                payload.clear()

        def remove() -> None:
            row = selected()
            if row and not row['read_only'] and self._confirm_dialog(dialog, '删除模型', f"删除连接“{row['name']}”？"):
                self.bridge.request('model_delete', model_id=row['id'])

        def update_actions() -> None:
            row = selected()
            editable = bool(row and not row['read_only'])
            edit_button.setEnabled(editable)
            delete_button.setEnabled(editable)

        buttons = QHBoxLayout()
        add_button = QPushButton('添加连接')
        add_button.setObjectName('primary')
        add_button.setIcon(icon('plus', '#253b59', 16))
        add_button.clicked.connect(lambda: edit(True))
        buttons.addWidget(add_button)
        edit_button = QPushButton('编辑')
        edit_button.clicked.connect(lambda: edit(False))
        buttons.addWidget(edit_button)
        delete_button = QPushButton('删除')
        delete_button.clicked.connect(remove)
        buttons.addWidget(delete_button)
        buttons.addStretch()
        close = QPushButton('关闭')
        close.clicked.connect(dialog.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        listing.itemSelectionChanged.connect(update_actions)
        refresh()
        self.bridge.state_changed.connect(refresh)
        try:
            dialog.exec()
        finally:
            self.bridge.state_changed.disconnect(refresh)

    def _delete(self) -> None:
        flow = self.state.get('selected_flow') if self.state else None
        if flow:
            if self.delete_button.isEnabled() and self._confirm_dialog(self, '删除 Flow', f'删除“{flow["title"]}”的流程记录？项目文件与节点会话保留。'):
                self.bridge.request('flow_delete', flow_id=flow['id'])
            return
        value = self.current_session
        if not value or not self.delete_button.isEnabled():
            return
        if self._confirm_dialog(self, '删除会话', f"将“{value['title']}”与其委派子 Agent 的记录移到回收区？项目文件会保留。"):
            self.bridge.request('delete', session_id=value['id'])

    @staticmethod
    def _confirm_dialog(parent: QWidget, title: str, text: str) -> bool:
        box = QMessageBox(parent)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText('确认')
        box.button(QMessageBox.StandardButton.No).setText('取消')
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _show_confirmation(self) -> None:
        value = self.current_session
        flow = self.state.get('selected_flow') if self.state else None
        if flow:
            run = flow.get('run') or {}
            identifier = run.get('nodes', {}).get(run.get('current_node'), {}).get('session_id')
            value = next((row for row in self.state['sessions'] if row['id'] == identifier), None)
        pending = value.get('confirmation') if value else None
        key = (value['id'], pending['id']) if pending else None
        if self._confirmation_key == key:
            return
        if self.confirmation_dialog is not None:
            self.confirmation_dialog.reject()
        self._confirmation_key = key
        self.confirmation_dialog = None
        if not pending:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"操作确认 · {value['title']}")
        dialog.setMinimumSize(450, 280)
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        layout.addWidget(_label('需要你的确认', 'dialogTitle'))
        layout.addWidget(_label('Agent 请求执行以下操作：', 'muted'))
        prompt = SafeTextBrowser()
        prompt.setOpenExternalLinks(False)
        prompt.setPlainText(pending['prompt'])
        layout.addWidget(prompt)
        buttons = QDialogButtonBox()
        deny = buttons.addButton('拒绝', QDialogButtonBox.ButtonRole.RejectRole)
        allow = buttons.addButton('允许本次', QDialogButtonBox.ButtonRole.AcceptRole)
        allow.setObjectName('primary')
        deny.setDefault(True)
        allow.setAutoDefault(False)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def decided(result: int) -> None:
            # 捕获会话和请求 ID；切换或新一轮确认不能继承旧批准。
            self.bridge.request('decide', session_id=key[0], confirmation_id=key[1], allowed=result == QDialog.DialogCode.Accepted)
            if self.confirmation_dialog is dialog:
                self.confirmation_dialog = None

        dialog.finished.connect(decided)
        self.confirmation_dialog = dialog
        dialog.open()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()
        if self.state.get('selected_flow'):
            self.flow_error.setText(message)
            self.flow_error.show()
        self.statusBar().showMessage(message, 15000)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shutdown_complete or not self.bridge.is_running:
            event.accept()
            return
        event.ignore()
        if self._closing or self._pending_close_drafts is not None:
            return
        drafts = self.flow_panel.pending_drafts()
        if drafts:
            box = QMessageBox(self)
            box.setWindowTitle('Flow 有未保存的修改')
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setText(f'{len(drafts)} 个 Flow 的画布或提示词尚未保存。')
            box.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            for button in box.buttons():
                button.setIcon(QIcon())
            box.button(QMessageBox.StandardButton.Save).setText('保存并退出')
            box.button(QMessageBox.StandardButton.Discard).setText('放弃修改')
            box.button(QMessageBox.StandardButton.Cancel).setText('继续编辑')
            box.setDefaultButton(QMessageBox.StandardButton.Save)
            decision = box.exec()
            if decision == QMessageBox.StandardButton.Cancel:
                return
            if decision == QMessageBox.StandardButton.Save:
                self._pending_close_drafts = {draft['id']: draft for draft in drafts}
                self.centralWidget().setEnabled(False)
                for draft in drafts:
                    self.bridge.request('flow_save', definition=draft)
                return
        self._begin_shutdown()

    def _begin_shutdown(self) -> None:
        self._closing = True
        if self.confirmation_dialog:
            self.confirmation_dialog.reject()
        self.phase_label.setText('正在停止任务并保存会话，请稍候…')
        self.composer.setEnabled(False)
        self._update_buttons()
        self.bridge.shutdown()

    def _stopped(self) -> None:
        self._shutdown_complete = True
        if self._closing:
            self.close()
        else:
            self.phase_label.setText('运行时已停止；请关闭窗口后重新启动。')
            self.centralWidget().setEnabled(False)
