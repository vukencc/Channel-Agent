"""原生 Agent Flow 画布与本地草稿。"""
from __future__ import annotations

import copy
import math
import os
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
import sys
import uuid

from PySide6.QtCore import QMimeData, QPointF, QRectF, QSaveFile, QIODevice, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QMessageBox, QFormLayout, QGraphicsItem, QGraphicsPathItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from .theme import ChoiceBox
from .project_picker import ProjectPicker
from . import project_picker
from .flow_designer import FlowDesigner
from agent_core.core.flow_handoff import HandoffContract


STATUS_LABELS = {
    'pending': '待运行', 'queued': '排队中', 'running': '运行中', 'completed': '已完成',
    'failed': '失败', 'cancelled': '已取消', 'cancelling': '正在取消', 'interrupted': '已中断',
    'confirming': '等待确认', 'stopping': '正在停止', 'idle': '空闲', 'checkpoint': '检查点',
    'persistence_failed': '完成状态保存失败，请检查存储后重试保存；节点不会重跑。',
}

CONTRACT_HINT = ('留空不配置；所有字段必填，不允许额外字段。number 不支持枚举；'
                 'integer 枚举限安全整数，字符串须为有效 Unicode。')


def _contract_draft(text: str):
    if not text.strip():
        return None
    try:
        value = json.loads(text)
        HandoffContract.model_validate(value)
        if isinstance(value, dict):
            return value
    except (ValueError, TypeError):
        pass
    return text


def _custom_tools_draft(text: str):
    if not text.strip():
        return []
    try:
        value = json.loads(text)
        if isinstance(value, list) and len({row.get('name') for row in value if isinstance(row, dict) and isinstance(row.get('name'), str)}) == len(value) and all(
            isinstance(row, dict) and isinstance(row.get('name'), str)
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', row['name'])
            and isinstance(row.get('description'), str) and bool(row['description']) and isinstance(row.get('parameters'), dict)
            and row['parameters'].get('type') == 'object' and isinstance(row.get('code'), str) and bool(row['code'])
            and isinstance(row.get('network', False), bool) for row in value
        ):
            return value
    except (ValueError, TypeError):
        pass
    return text


def _status_text(status) -> str:
    return STATUS_LABELS.get(status, str(status)) if status else '未运行'


def _status_color(status) -> str:
    if status in ('completed',):
        return '#7ad2b0'
    if status in ('failed', 'interrupted', 'error', 'persistence_failed'):
        return '#e49aa7'
    if status in ('running', 'queued', 'confirming', 'cancelling'):
        return '#a9caff'
    return '#92a7c0'


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class AddNodeButton(QPushButton):
    """单击添加，或拖到画布中的指定位置。"""
    def mousePressEvent(self, event) -> None:
        self._start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton and (event.position().toPoint() - self._start).manhattanLength() >= 10:
            mime = QMimeData()
            mime.setData('application/x-agent-flow-node', b'agent')
            drag = QDrag(self)
            drag.setMimeData(mime)
            self.setDown(False)
            drag.exec(Qt.DropAction.CopyAction)
            return
        super().mouseMoveEvent(event)


class NodeItem(QGraphicsItem):
    def __init__(self, panel: FlowPanel, data: dict):
        super().__init__()
        self.panel, self.data = panel, data
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setPos(float(data.get('x', 0)), float(data.get('y', 0)))

    def boundingRect(self) -> QRectF:
        return QRectF(-9, -4, 218, 98)

    def paint(self, painter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#90b5eb' if self.isSelected() else '#455468'), 1.5))
        painter.setBrush(QColor('#27303c'))
        painter.drawRoundedRect(QRectF(0, 0, 200, 90), 3, 3)
        painter.setPen(QColor('#e1eaf6'))
        font = painter.font()
        font.setPixelSize(14)
        painter.setFont(font)
        painter.drawText(QRectF(14, 10, 172, 25), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(str(self.data.get('title') or 'AgentCore'),
                                                          Qt.TextElideMode.ElideRight, 172))
        result = (self.panel._flow.get('run') or {}).get('nodes', {}).get(self.data['id'], {})
        status = result.get('status') or 'pending'
        session_status = result.get('session_status')
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_status_color(status)))
        painter.drawEllipse(QPointF(18, 54), 3, 3)
        painter.setPen(QColor('#92a7c0'))
        font.setPixelSize(12)
        painter.setFont(font)
        text = _status_text(status)
        if status == 'running' and session_status:
            text += ' · ' + _status_text(session_status)
        painter.drawText(QRectF(28, 42, 158, 24), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, 158))
        painter.setPen(QPen(QColor('#9abce9'), 1.5))
        painter.setBrush(QColor('#263c56'))
        painter.drawEllipse(QPointF(0, 45), 6, 6)
        painter.drawEllipse(QPointF(200, 45), 6, 6)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and hasattr(self, 'panel'):
            if self.scene() and not self.panel._loading:
                self.data['x'], self.data['y'] = value.x(), value.y()
                self.panel._mark_dirty()
                self.panel._update_edges()
        return super().itemChange(change, value)


class EdgeItem(QGraphicsPathItem):
    def __init__(self, source: NodeItem, target: NodeItem, data: dict):
        super().__init__()
        self.source, self.target, self.data = source, target, data
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(-1)
        self.refresh()

    def refresh(self) -> None:
        a, b = self.source.mapToScene(QPointF(200, 45)), self.target.mapToScene(QPointF(0, 45))
        dx = max(70, abs(b.x() - a.x()) * .5)
        path = QPainterPath(a)
        path.cubicTo(a + QPointF(dx, 0), b - QPointF(dx, 0), b)
        self.setPath(path)

    def paint(self, painter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#d1e4ff' if self.isSelected() else '#729acb'), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())
        end = self.path().pointAtPercent(1)
        before = self.path().pointAtPercent(.97)
        angle = math.atan2(end.y() - before.y(), end.x() - before.x())
        points = [end, end - QPointF(10 * math.cos(angle - .48), 10 * math.sin(angle - .48)),
                  end - QPointF(10 * math.cos(angle + .48), 10 * math.sin(angle + .48))]
        painter.setBrush(QColor('#9bbce8'))
        painter.drawPolygon(QPolygonF(points))


class FlowCanvas(QGraphicsView):
    def __init__(self, panel: FlowPanel):
        super().__init__(panel)
        self.panel = panel
        self.setScene(QGraphicsScene(self))
        self.setSceneRect(-2000, -2000, 4000, 4000)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setStyleSheet('QGraphicsView { background: #1b2027; border: none; }')
        self._source = None
        self._preview = None
        self._pan = None

    def dragEnterEvent(self, event) -> None:
        if not self.panel.running and event.mimeData().hasFormat('application/x-agent-flow-node'):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if not self.panel.running and event.mimeData().hasFormat('application/x-agent-flow-node'):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        if not self.panel.running and event.mimeData().hasFormat('application/x-agent-flow-node'):
            self.panel.add_node(self.mapToScene(event.position().toPoint()))
            event.acceptProposedAction()
        else:
            event.ignore()

    def drawBackground(self, painter, rect) -> None:
        painter.fillRect(rect, QColor('#1b2027'))
        painter.setPen(QPen(QColor('#34404f'), 2))
        step = 24
        for x in range(math.floor(rect.left() / step) * step, math.ceil(rect.right()), step):
            for y in range(math.floor(rect.top() / step) * step, math.ceil(rect.bottom()), step):
                painter.drawPoint(x, y)

    def _port_node(self, pos, output: bool):
        point = self.mapToScene(pos)
        for node in self.panel.nodes.values():
            port = node.mapToScene(QPointF(200 if output else 0, 45))
            if math.hypot(point.x() - port.x(), point.y() - port.y()) <= 12:
                return node
        return None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton and not self.panel.running:
            self._source = self._port_node(event.position().toPoint(), True)
            if self._source:
                self._preview = self.scene().addPath(QPainterPath(), QPen(QColor('#9bbce8'), 2, Qt.PenStyle.DashLine))
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._pan is not None:
            point = event.position().toPoint()
            delta = point - self._pan
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._pan = point
            return
        if self._source and self._preview:
            a = self._source.mapToScene(QPointF(200, 45))
            b = self.mapToScene(event.position().toPoint())
            path = QPainterPath(a)
            path.cubicTo(a + QPointF(80, 0), b - QPointF(80, 0), b)
            self._preview.setPath(path)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._pan is not None:
            self._pan = None
            self.unsetCursor()
            return
        if self._source:
            target = self._port_node(event.position().toPoint(), False)
            if target and target is not self._source:
                self.panel.add_edge(self._source.data['id'], target.data['id'])
            if self._preview:
                self.scene().removeItem(self._preview)
            self._source, self._preview = None, None
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if not self.itemAt(event.position().toPoint()) and not self.panel.running:
            self.panel.add_node(self.mapToScene(event.position().toPoint()))
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not self.panel.running:
            self.panel.delete_selected()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
            return
        super().wheelEvent(event)


class FlowPanel(QWidget):
    command = Signal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._flow, self._models, self._cache = {}, [], {}
        self._dirty, self._loading, self._selected = False, False, None
        self._submitted = None
        self._queued_input = None
        self._run_input_submitted = None
        self._run_inputs = {}
        self._project_dirty = False
        self._project_dirty_cache = {}
        self._pending_project_action = None
        self._directory_update_id = None
        self._project_rows = {}
        self.nodes, self.edges = {}, []
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        header = QHBoxLayout()
        self.heading, self.status = _label('Agent Flow'), _label('')
        self.heading.setObjectName('title')
        header.addWidget(self.heading, 1)
        header.addWidget(self.status)
        self.designer_button = QPushButton('设计助手')
        self.designer_button.setCheckable(True)
        self.import_button = QPushButton('导入')
        self.export_button = QPushButton('导出')
        header.addWidget(self.import_button)
        header.addWidget(self.export_button)
        header.addWidget(self.designer_button)
        root.addLayout(header)
        project_row = QHBoxLayout()
        self.project_summary = _label('未选择项目')
        self.project_summary.setWordWrap(True)
        self.project_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.project_button = QPushButton('选择项目')
        self.project_button.clicked.connect(self._choose_project)
        project_row.addWidget(self.project_summary, 1)
        project_row.addWidget(self.project_button)
        root.addLayout(project_row)
        controls = QHBoxLayout()
        self.add_button = AddNodeButton('添加 AgentCore')
        self.save_button = QPushButton('保存')
        self.run_button = QPushButton('运行')
        self.fit_button = QPushButton('适配')
        self.inspector_button = QPushButton('检查器')
        for button in (self.add_button, self.save_button, self.run_button, self.fit_button):
            controls.addWidget(button)
        controls.addStretch(1)
        controls.addWidget(self.inspector_button)
        root.addLayout(controls)
        run_input_row = QHBoxLayout()
        self.run_input = QTextEdit()
        self.run_input.setAcceptRichText(False)
        self.run_input.setFixedHeight(42)
        self.run_input.setPlaceholderText('本次运行补充信息（可选）')
        self.queue_button = QPushButton('排队')
        self.queue_count = _label('排队：0')
        run_input_row.addWidget(self.run_input, 1)
        run_input_row.addWidget(self.queue_button)
        run_input_row.addWidget(self.queue_count)
        root.addLayout(run_input_row)
        self.queue_button.clicked.connect(self._queue_run)
        body = QHBoxLayout()
        root.addLayout(body, 1)
        self.canvas = FlowCanvas(self)
        body.addWidget(self.canvas, 1)
        self.inspector = QTabWidget()
        self.inspector.setMinimumWidth(270)
        self.inspector.setMaximumWidth(340)
        body.addWidget(self.inspector)
        self.designer = FlowDesigner()
        self.designer.setMinimumWidth(210)
        self.designer.setMaximumWidth(340)
        self.designer.hide()
        body.addWidget(self.designer)
        self.designer.command.connect(self.command.emit)
        self.designer_button.toggled.connect(self._toggle_designer)
        self.flow_fields = self._make_form([
            ('名称', 'title', 'line'), ('项目目录', 'workspace_path', 'line'),
            ('项目背景（可选）', 'handoff', 'text'), ('执行环境', 'execution_environment', 'environment'),
            ('权限模式', 'permission_policy', 'policy'), ('模型', 'model_profile', 'model')])
        self.node_fields = self._make_form([
            ('名称', 'title', 'line'), ('节点指令', 'prompt', 'text'), ('结构化交接契约（可选）', 'handoff_contract', 'text'), ('复用会话', 'reuse_session', 'checkbox'),
            ('项目目录', 'workspace_path', 'line'), ('补充背景（可选）', 'handoff', 'text'),
            ('已有交接文件（可选）', 'handoff_file', 'line'), ('模型', 'model_profile', 'model'),
            ('思考模式', 'thinking_mode', 'thinking'), ('推理强度', 'reasoning_effort', 'effort')])
        self.tools_toggle = QToolButton()
        self.tools_toggle.setText('节点工具')
        self.tools_toggle.setCheckable(True)
        self.node_form.insertWidget(self.node_form.count() - 1, self.tools_toggle)
        self.tools_container = QWidget()
        self.tools_layout = QVBoxLayout(self.tools_container)
        self.tools_layout.setContentsMargins(0, 0, 0, 0)
        self.tools_mode = ChoiceBox()
        self.tools_mode.addItem('继承部署工具', None)
        self.tools_mode.addItem('自定义工具', 'custom')
        self.tools_layout.addWidget(self.tools_mode)
        self.tool_checks = {}
        self.custom_tool_checks = {}
        self.custom_tools_label = _label('动态工具定义（JSON 数组）')
        self.custom_tools_edit = QTextEdit()
        self.custom_tools_edit.setAcceptRichText(False)
        self.custom_tools_edit.setPlaceholderText('[{"name":"example","description":"说明","parameters":{"type":"object","properties":{}},"code":"def main(arguments):\\n    return {}","network":false}]')
        self.custom_tools_hint = _label('Python 代码须定义 main(arguments)，返回可序列化的 JSON。')
        self.tools_layout.addWidget(self.custom_tools_label)
        self.tools_layout.addWidget(self.custom_tools_edit)
        self.tools_layout.addWidget(self.custom_tools_hint)
        self.custom_tools_edit.textChanged.connect(self._custom_tools_changed)
        self.node_form.insertWidget(self.node_form.count() - 1, self.tools_container)
        self.tools_container.hide()
        self.tools_toggle.toggled.connect(self.tools_container.setVisible)
        self.tools_mode.currentIndexChanged.connect(self._tools_changed)
        self.import_button.clicked.connect(self._import_file)
        self.export_button.clicked.connect(self._export_file)
        self._export_path = None
        self.prompt_edit = self.node_fields['prompt']
        result_scroll = QScrollArea()
        result_scroll.setWidgetResizable(True)
        result_container = QWidget()
        result_layout = QVBoxLayout(result_container)
        self.result_label = _label('')
        self.result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result_label.setWordWrap(True)
        result_layout.addWidget(self.result_label)
        self.session_button = QPushButton('查看会话')
        result_layout.addWidget(self.session_button)
        result_layout.addStretch()
        result_scroll.setWidget(result_container)
        self.inspector.addTab(result_scroll, '运行')
        self.session_button.clicked.connect(self._select_session)
        self.add_button.clicked.connect(lambda: self.add_node())
        self.save_button.clicked.connect(lambda: self._submit('flow_save'))
        self.run_button.clicked.connect(self._run_or_stop)
        self.fit_button.clicked.connect(self._fit)
        self.inspector_button.clicked.connect(self._toggle_inspector)
        self.canvas.scene().selectionChanged.connect(self._selection_changed)

    def _toggle_designer(self, visible: bool) -> None:
        self.designer.setMaximumWidth(230 if self.width() < 750 else 340)
        self.designer.setVisible(visible)
        if visible and self.width() < 750:
            self.inspector.hide()
            self.canvas.show()
        if not visible:
            self.designer.cancel()

    def _make_form(self, rows):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        layout = QVBoxLayout(container)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        layout.addLayout(form)
        explanation = _label('上游完成后会自动把结果和待办交给下游，无需手填。')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        toggle = QToolButton()
        toggle.setText('可选补充背景')
        toggle.setCheckable(True)
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        layout.addWidget(toggle)
        context = QWidget()
        context_form = QFormLayout(context)
        context_form.setContentsMargins(0, 0, 0, 0)
        context_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        layout.addWidget(context)
        toggle.toggled.connect(lambda checked: (context.setVisible(checked), toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)))
        context.hide()
        toggle.setArrowType(Qt.ArrowType.RightArrow)
        fields = {}
        is_node = hasattr(self, 'flow_fields')
        if is_node:
            self.node_context_toggle = toggle
        else:
            self.flow_context_toggle = toggle
        for caption, key, kind in rows:
            if kind == 'checkbox':
                field = QCheckBox()
                field.toggled.connect(lambda value, k=key, n=is_node: self._field_changed(k, value, n))
            elif kind == 'text':
                field = QTextEdit()
                field.setAcceptRichText(False)
                field.setMinimumHeight(90)
                if key == 'handoff_contract':
                    field.setPlaceholderText('留空不配置。例：{"fields":{"answer":{"type":"string"}}}')
                    field.setMaximumHeight(130)
                if key == 'handoff':
                    field.setMaximumHeight(130)
                elif key == 'prompt':
                    field.setPlaceholderText('定义这个节点的职责与要完成的任务。')
                    field.setMaximumHeight(130)
                field.textChanged.connect(lambda k=key, f=field, n=is_node: self._field_changed(k, f.toPlainText(), n))
            elif key == 'workspace_path':
                picker = ProjectPicker(guard=lambda n=is_node: self._picker_token(n))
                field = picker.path_edit
                field.setPlaceholderText('留空继承上游或 Flow 项目' if is_node else '选择已有项目目录')
                field.textChanged.connect(lambda value, k=key, n=is_node: self._field_changed(k, value, n))
                if is_node:
                    self.node_project_picker = picker
                else:
                    self.flow_project_picker = picker
            elif kind == 'line':
                field = QLineEdit()
                field.textChanged.connect(lambda value, k=key, n=is_node: self._field_changed(k, value, n))
            else:
                field = ChoiceBox()
                choices = [('继承', None)]
                if kind == 'environment':
                    choices += [('WSL 隔离沙箱' if sys.platform == 'win32' else '隔离沙箱', 'sandbox')]
                    if sys.platform == 'linux':
                        choices.append(('Linux 本机', 'local'))
                elif kind == 'policy':
                    choices += [('智能申请', 'smart'), ('只读', 'readonly'), ('完全访问', 'full_access')]
                elif kind == 'thinking':
                    choices += [(value, value) for value in ('auto', 'enabled', 'disabled')]
                elif kind == 'effort':
                    choices += [(value, value) for value in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')]
                for label, value in choices:
                    field.addItem(label, value)
                field.currentIndexChanged.connect(lambda _, k=key, f=field, n=is_node: self._field_changed(k, f.currentData(), n))
            fields[key] = field
            target_form = context_form if key in ('handoff', 'handoff_file') else form
            if key == 'workspace_path' and is_node:
                project_container = QWidget()
                project_layout = QVBoxLayout(project_container)
                project_layout.setContentsMargins(0, 0, 0, 0)
                project_layout.setSpacing(8)
                project_layout.addWidget(picker)
                self.node_project_summary = _label('留空时继承项目')
                self.node_project_summary.setWordWrap(True)
                self.node_project_summary.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
                project_layout.addWidget(self.node_project_summary)
                target_form.addRow(_label(caption), project_container)
                target_form.setRowVisible(project_container, False)
            else:
                target_form.addRow(_label(caption), picker if key == 'workspace_path' else field)
                if key == 'handoff_contract':
                    self.contract_hint = _label(CONTRACT_HINT)
                    target_form.addRow(self.contract_hint)
                if not is_node and key in ('execution_environment', 'permission_policy'):
                    self._project_rows[key] = (target_form, field)
                if key in ('workspace_path', 'handoff_file', 'execution_environment', 'permission_policy'):
                    target_form.setRowVisible(picker if key == 'workspace_path' else field, False)
        layout.addStretch()
        scroll.setWidget(container)
        self.inspector.addTab(scroll, '节点' if is_node else 'Flow')
        if is_node:
            self.node_form = layout
        return fields

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def running(self) -> bool:
        return (self._flow.get('run') or {}).get('status') in ('running', 'queued', 'cancelling')

    def draft(self) -> dict:
        definition = copy.deepcopy(self._flow)
        definition.pop('pending_runs', None)
        definition.pop('session_bindings', None)
        return definition

    def pending_drafts(self) -> list[dict]:
        """返回尚未保存的所有 Flow 草稿，不暴露内部对象。"""
        drafts = {key: value for key, (value, dirty) in self._cache.items() if dirty}
        current_id = self._flow.get('id')
        if current_id:
            drafts.pop(current_id, None)
            if self._dirty:
                drafts[current_id] = self._flow
        result = copy.deepcopy(list(drafts.values()))
        for draft in result:
            draft.pop('pending_runs', None)
            draft.pop('session_bindings', None)
        return result

    def project_is_dirty(self, flow_id: str) -> bool:
        return self._project_dirty if self._flow.get('id') == flow_id else self._project_dirty_cache.get(flow_id, False)

    def project_saved(self, flow_id: str, project: dict) -> None:
        if self._flow.get('id') == flow_id:
            self._flow['project'] = copy.deepcopy(project)
            self._project_dirty = False
        elif flow_id in self._cache:
            self._cache[flow_id][0]['project'] = copy.deepcopy(project)
            self._project_dirty_cache[flow_id] = False

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._refresh_header()
        self._refresh_projects()

    def _picker_token(self, node: bool):
        if not self._flow or self.running or (node and not self._selected):
            return None
        return (self._flow.get('id'), self._selected if node else None)

    def _choose_project(self) -> None:
        token = self._picker_token(False)
        if token is None:
            return
        project = self._flow.get('project') or {}
        directory = project_picker.choose_project_directory(self, str(project.get('workspace_path') or ''))
        if directory and token == self._picker_token(False):
            self._directory_update_id = (self._flow['id'], self._flow['project_id'])
            self._refresh_state()
            self.command.emit('project_update', {'project_id': self._flow['project_id'], 'changes': {'workspace_path': directory, 'revision': project.get('revision')}})

    def _refresh_projects(self) -> None:
        path = str((self._flow.get('project') or {}).get('workspace_path') or '')
        if path:
            summary = f'项目工作区：{os.path.basename(path.rstrip("/")) or path}\n{path}'
        else:
            targets = {edge.get('target') for edge in self._flow.get('edges', [])}
            roots = [node for node in self._flow.get('nodes', []) if node.get('id') not in targets]
            if roots and all(node.get('workspace_path') for node in roots):
                summary = '项目工作区：未选择项目；运行前由 Core 校验。'
            else:
                summary = '项目工作区：未选择项目。'
        self.project_summary.setText(summary)
        if not self._flow.get('project_id'):
            self.project_summary.setText('Flow 模板 · 导入项目后运行')
        self.project_button.setText('更换项目' if path else '选择项目')
        if not self._selected or self._selected not in self.nodes:
            self.node_project_summary.setText('留空时继承项目')
            return
        incoming = {}
        for edge in self._flow.get('edges', []):
            incoming.setdefault(edge.get('target'), []).append(edge.get('source'))

        memo = {}
        visiting = set()

        def resolve(key):
            if key in memo:
                return memo[key]
            if key in visiting or key not in self.nodes:
                return None, '项目继承存在循环或无效节点'
            visiting.add(key)
            node = self.nodes[key].data
            parents = incoming.get(key, [])
            own = str(node.get('workspace_path') or '')
            candidate = own or (path if not parents else '')
            parsed = (PureWindowsPath(candidate) if sys.platform == 'win32' else PurePosixPath(candidate)) if candidate else None
            # 这里只预览路径语法；实际目录、身份与授权仍由 Core 校验。
            invalid = parsed is not None and (not parsed.is_absolute() or '..' in parsed.parts
                                              or (sys.platform == 'win32' and (len(parsed.drive) != 2 or parsed.drive[1] != ':'))
                                              or (sys.platform != 'win32' and parsed.anchor == '//'))
            if invalid:
                result = None, '项目路径不合法：请选择绝对目录，不能包含 .. 或网络路径。'
            elif not parents:
                value = str(parsed) if parsed is not None else ''
                result = (value, '本节点' if own else 'Flow') if value else (None, '尚未选择项目')
            else:
                results = [resolve(parent) for parent in parents]
                if any(not value for value, _ in results):
                    result = None, next(reason for value, reason in results if not value)
                else:
                    values = {value for value, _ in results}
                    normalized_own = str(parsed) if own else ''
                    if len(values) != 1 or (own and normalized_own not in values):
                        result = None, '项目冲突，请检查本节点及上游项目'
                    else:
                        result = (normalized_own, '本节点') if own else (results[0][0], '上游')
            visiting.remove(key)
            memo[key] = result
            return result

        value, source = resolve(self._selected)
        self.node_project_summary.setText(f'生效项目（{source}）：\n{value}' if value else source)

    def _field_changed(self, key, value, node) -> None:
        if self._loading or self.running or not self._flow:
            return
        if key == 'handoff_contract':
            value = _contract_draft(value)
            self.contract_hint.setText('契约无效，请修正或清空。' if isinstance(value, str) else CONTRACT_HINT)
        project_field = not node and self._flow.get('project_id') and key in ('handoff', 'execution_environment', 'permission_policy', 'model_profile')
        target = self._flow.get('project') if project_field else (self.nodes.get(self._selected).data if node and self._selected in self.nodes else (None if node else self._flow))
        if target is not None and target.get(key) != value:
            target[key] = value
            if project_field:
                self._project_dirty = True
            self._mark_dirty()
            if node:
                self.nodes[self._selected].update()

    def _fill_fields(self, fields, data) -> None:
        for key, field in fields.items():
            value = data.get(key)
            if isinstance(field, QCheckBox):
                field.setChecked(bool(value))
            elif isinstance(field, QComboBox):
                index = field.findData(value)
                field.setCurrentIndex(max(0, index))
            elif isinstance(field, QTextEdit):
                if key == 'handoff_contract' and isinstance(value, dict):
                    value = json.dumps(value, ensure_ascii=False, indent=2)
                field.setPlainText(str(value or ''))
                if key == 'handoff_contract':
                    self.contract_hint.setText('契约无效，请修正或清空。' if isinstance(data.get(key), str) else CONTRACT_HINT)
            else:
                field.setText(str(value or ''))
        if fields is self.node_fields:
            names = data.get('tool_names')
            self.tools_mode.setCurrentIndex(0 if names is None else 1)
            for name, check in self.tool_checks.items():
                check.setChecked(bool(check.property('required')) or names is None or name in names)
                check.setEnabled(names is not None and not check.property('required') and not self.running)
            definitions = data.get('custom_tools', [])
            self.custom_tools_edit.setPlainText(definitions if isinstance(definitions, str) else json.dumps(definitions or [], ensure_ascii=False, indent=2))
            self.custom_tools_hint.setText('定义无效，请修正。' if isinstance(definitions, str) else 'Python 代码须定义 main(arguments)，返回可序列化的 JSON。')
            self._refresh_custom_checks(definitions, names)

    def _refresh_custom_checks(self, definitions, names) -> None:
        for check in self.custom_tool_checks.values():
            self.tools_layout.removeWidget(check)
            check.deleteLater()
        self.custom_tool_checks = {}
        if not isinstance(definitions, list):
            return
        for row in definitions:
            if not isinstance(row, dict) or not isinstance(row.get('name'), str):
                continue
            name = row['name']
            check = QCheckBox(name + '（自定义）')
            check.setToolTip(str(row.get('description') or ''))
            check.setChecked(names is None or name in names)
            check.setEnabled(names is not None and not self.running and bool(self._selected))
            check.toggled.connect(self._tools_changed)
            self.tools_layout.addWidget(check)
            self.custom_tool_checks[name] = check

    def _custom_tools_changed(self) -> None:
        if self._loading or self.running or self._selected not in self.nodes:
            return
        value = _custom_tools_draft(self.custom_tools_edit.toPlainText())
        self.custom_tools_hint.setText('定义无效，请修正。' if isinstance(value, str) else 'Python 代码须定义 main(arguments)，返回可序列化的 JSON。')
        names = self.nodes[self._selected].data.get('tool_names')
        if isinstance(value, list) and names is not None:
            known = set(self.tool_checks) | {item['name'] for item in value}
            self._field_changed('tool_names', [name for name in names if name in known], True)
        self._field_changed('custom_tools', value, True)
        self._refresh_custom_checks(value, self.nodes[self._selected].data.get('tool_names'))

    def set_tool_catalog(self, catalog: dict) -> None:
        rows = catalog.get('tools', [])
        if rows == getattr(self, '_tool_rows', None):
            return
        self._tool_rows = copy.deepcopy(rows)
        for check in self.tool_checks.values():
            self.tools_layout.removeWidget(check)
            check.deleteLater()
        self.tool_checks = {}
        for row in rows:
            name = row['name']
            check = QCheckBox(name + ('（基础）' if row.get('required') else ''))
            check.setToolTip(str(row.get('description') or ''))
            check.setProperty('required', bool(row.get('required')))
            check.toggled.connect(self._tools_changed)
            self.tools_layout.addWidget(check)
            self.tool_checks[name] = check
        self._loading = True
        self._fill_fields(self.node_fields, self.nodes[self._selected].data if self._selected in self.nodes else {})
        self._loading = False

    def _tools_changed(self, *_args) -> None:
        custom = self.tools_mode.currentData() == 'custom'
        if not custom:
            for check in (*self.tool_checks.values(), *self.custom_tool_checks.values()):
                blocker = QSignalBlocker(check)
                check.setChecked(True)
                del blocker
        for check in self.tool_checks.values():
            check.setEnabled(custom and not check.property('required') and not self.running and bool(self._selected))
        for check in self.custom_tool_checks.values():
            check.setEnabled(custom and not self.running and bool(self._selected))
        selected_names = (self.nodes[self._selected].data.get('tool_names') or []) if self._selected in self.nodes else []
        known = set(self.tool_checks) | set(self.custom_tool_checks)
        names = ([name for name in selected_names if name not in known] +
                 [name for name, check in {**self.tool_checks, **self.custom_tool_checks}.items() if check.isChecked()]) if custom else None
        self._field_changed('tool_names', names, True)

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, '导入 Flow 模板', '', 'JSON (*.json)')
        if not path:
            return
        try:
            with open(path, 'rb') as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('文件超过 1 MiB。')
            payload = json.loads(raw.decode('utf-8'))
            known = {row.get('id') for row in self._models if isinstance(row.get('id'), str)}
            values = [payload] if isinstance(payload, dict) else []
            if isinstance(payload, dict) and isinstance(payload.get('nodes'), list):
                values.extend(row for row in payload['nodes'] if isinstance(row, dict))
            missing = any(isinstance(row.get('model_profile'), str) and row['model_profile'] and row['model_profile'] not in known for row in values)
            choice = QMessageBox.StandardButton.No
            if missing:
                choice = QMessageBox.question(self, '缺失模型', '模板引用的模型不存在，改为继承模型？\n选择“否”将严格检查模型。',
                                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            self.command.emit('flow_import', {'payload': payload, 'missing_models': 'inherit' if choice == QMessageBox.StandardButton.Yes else 'error'})
        except (OSError, ValueError, UnicodeError) as exc:
            QMessageBox.warning(self, '导入失败', str(exc))

    def _contract_valid(self) -> bool:
        if any(isinstance(node.get('handoff_contract'), str) for node in self._flow.get('nodes', [])):
            QMessageBox.warning(self, '契约无效', '结构化交接契约无效，请修正或清空后重试。')
            return False
        if any(isinstance(node.get('custom_tools'), str) for node in self._flow.get('nodes', [])):
            QMessageBox.warning(self, '动态工具无效', '动态工具定义无效，请修正或清空后重试。')
            return False
        return True

    def _export_file(self) -> None:
        if not self._contract_valid():
            return
        if self._export_path is not None:
            return
        path, _ = QFileDialog.getSaveFileName(self, '导出 Flow 模板', '', 'JSON (*.json)')
        if path:
            self._export_path = path
            self.export_button.setEnabled(False)
            definition = self.draft()
            definition.pop('project', None)
            self.command.emit('flow_export', {'definition': definition})

    def set_flow(self, flow: dict | None, models: list[dict] | None = None) -> None:
        incoming = copy.deepcopy(flow or {})
        old_id, new_id = self._flow.get('id'), incoming.get('id')
        if models is not None and models != self._models:
            self._models = copy.deepcopy(models)
            self.designer.set_models(models)
            self._loading = True
            for fields in (self.flow_fields, self.node_fields):
                box = fields['model_profile']
                current = box.currentData()
                box.clear()
                box.addItem('继承', None)
                for row in self._models:
                    box.addItem(str(row.get('name') or row.get('id') or ''), row.get('id'))
                box.setCurrentIndex(max(0, box.findData(current)))
            self._loading = False
        if (old_id == new_id and incoming.get('revision') is not None
                and self._flow.get('revision') is not None
                and incoming['revision'] < self._flow['revision']):
            return
        if old_id != new_id:
            if old_id:
                self._run_inputs[old_id] = self.run_input.toPlainText()
            self.run_input.setPlainText(self._run_inputs.get(new_id, ''))
            if old_id:
                self._project_dirty_cache[old_id] = self._project_dirty
            self._project_dirty = self._project_dirty_cache.get(new_id, False)
            if old_id:
                self._cache[old_id] = (copy.deepcopy(self._flow), self._dirty)
            cached = self._cache.get(new_id)
            if cached and cached[1]:
                self._flow, self._dirty = copy.deepcopy(cached[0]), True
                self._flow['run'] = incoming.get('run')
                self._flow['pending_runs'] = incoming.get('pending_runs', [])
            else:
                self._flow, self._dirty = incoming, False
            self._rebuild()
        elif (not self._dirty and incoming.get('revision') is not None
              and (self._flow.get('revision') is None or incoming['revision'] > self._flow['revision'])):
            self._flow = incoming
            self._rebuild(preserve_selection=True)
        else:
            self._flow['run'] = incoming.get('run')
            self._flow['pending_runs'] = incoming.get('pending_runs', [])
        self._refresh_state()

    def _rebuild(self, preserve_selection: bool = False) -> None:
        selected_id = self._selected if preserve_selection else None
        self._loading = True
        scene = self.canvas.scene()
        try:
            with QSignalBlocker(scene):
                scene.clear()
                self.canvas._source, self.canvas._preview = None, None
                self.nodes, self.edges, self._selected = {}, [], None
                for row in self._flow.get('nodes', []):
                    item = NodeItem(self, row)
                    self.nodes[row['id']] = item
                    scene.addItem(item)
                for row in self._flow.get('edges', []):
                    if row.get('source') in self.nodes and row.get('target') in self.nodes:
                        edge = EdgeItem(self.nodes[row['source']], self.nodes[row['target']], row)
                        scene.addItem(edge)
                        self.edges.append(edge)
                selected = self.nodes.get(selected_id)
                if selected:
                    selected.setSelected(True)
                    self._selected = selected_id
                project = self._flow.get('project') or {}
                values = dict(self._flow)
                if self._flow.get('project_id'):
                    values.update({key: project.get(key) for key in ('handoff', 'execution_environment', 'permission_policy', 'model_profile')})
                self._fill_fields(self.flow_fields, values)
                self._fill_fields(self.node_fields, selected.data if selected else {})
                self.flow_context_toggle.setChecked(bool(values.get('handoff')))
                self.node_context_toggle.setChecked(bool(selected and (selected.data.get('handoff') or selected.data.get('handoff_file'))))
        finally:
            self._loading = False

    def _refresh_header(self) -> None:
        title = (self._flow.get('project') or {}).get('title') or self._flow.get('title') or 'Agent Flow'
        self.heading.setText(str(title) + (' *' if self._dirty else ''))
        self.status.setText(_status_text((self._flow.get('run') or {}).get('status')))

    def _refresh_state(self) -> None:
        self._refresh_header()
        running = self.running
        self.add_button.setEnabled(bool(self._flow) and not running)
        updating_project = bool(self._pending_project_action or self._directory_update_id)
        self.save_button.setEnabled(bool(self._flow) and not running and not updating_project)
        self.export_button.setEnabled(bool(self._flow) and self._export_path is None)
        self.run_button.setVisible(bool(self._flow.get('project_id')) or running)
        self.inspector.setTabVisible(2, bool(self._flow.get('project_id')))
        self.project_button.setVisible(bool(self._flow.get('project_id')))
        for form, field in self._project_rows.values():
            form.setRowVisible(field, bool(self._flow.get('project_id')))
        self.run_button.setEnabled((bool(self._flow.get('project_id')) and not updating_project) or running)
        retry_completion = (self._flow.get('run') or {}).get('status') == 'persistence_failed'
        self.run_button.setText('停止' if running else '重试保存' if retry_completion else '运行')
        has_project = bool(self._flow.get('project_id'))
        self.run_input.setVisible(has_project)
        self.queue_count.setVisible(has_project)
        self.queue_count.setText(f"排队：{len(self._flow.get('pending_runs') or [])}")
        can_queue = (self._flow.get('run') or {}).get('status') in ('queued', 'running')
        self.queue_button.setVisible(has_project and can_queue and any(node.get('reuse_session') for node in self._flow.get('nodes', [])))
        self.queue_button.setEnabled(self._queued_input is None)
        self.project_button.setEnabled(bool(self._flow) and not running and not updating_project)
        self.flow_project_picker.browse_button.setEnabled(bool(self._flow) and not running)
        self.node_project_picker.browse_button.setEnabled(bool(self._flow) and not running and bool(self._selected))
        for item in self.nodes.values():
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not running)
            item.update()
        for fields, enabled in ((self.flow_fields, not running), (self.node_fields, not running and bool(self._selected))):
            for field in fields.values():
                if isinstance(field, (QTextEdit, QLineEdit)):
                    field.setReadOnly(not enabled)
                else:
                    field.setEnabled(enabled)
        self.tools_mode.setEnabled(not running and bool(self._selected))
        self.custom_tools_edit.setReadOnly(running or not bool(self._selected))
        for check in self.tool_checks.values():
            check.setEnabled(not running and bool(self._selected) and self.tools_mode.currentData() == 'custom' and not check.property('required'))
        for check in self.custom_tool_checks.values():
            check.setEnabled(not running and bool(self._selected) and self.tools_mode.currentData() == 'custom')
        self._refresh_projects()
        self._show_result()

    def _selection_changed(self) -> None:
        if self._loading:
            return
        selected = next((item for item in self.canvas.scene().selectedItems() if isinstance(item, NodeItem)), None)
        self._selected = selected.data['id'] if selected else None
        self._loading = True
        self._fill_fields(self.node_fields, selected.data if selected else {})
        self.node_context_toggle.setChecked(bool(selected and (selected.data.get('handoff') or selected.data.get('handoff_file'))))
        self._loading = False
        if selected:
            results = (self._flow.get('run') or {}).get('nodes', {})
            self.inspector.setCurrentIndex(2 if self._selected in results else 1)
        self._refresh_state()

    def _show_result(self) -> None:
        run = self._flow.get('run') or {}
        result = run.get('nodes', {}).get(self._selected, {})
        parts = []
        if run.get('input_text'):
            parts.append(f"本次补充信息：\n{run['input_text']}")
        for title, key in [('状态', 'status'), ('项目目录', 'workspace_path'), ('输入交接', 'received_handoff'), ('输出交接', 'output_handoff'), ('错误', 'error')]:
            if result.get(key):
                value = _status_text(result[key]) if key == 'status' else result[key]
                parts.append(f"{title}：\n{value}")
        validation = result.get('handoff_validation')
        if validation or (self._selected in self.nodes and self.nodes[self._selected].data.get('handoff_contract')):
            value = ('尚未校验' if not validation else '格式校验通过（不代表业务验收通过）' if validation.get('status') == 'passed'
                     else '校验失败：' + str(validation.get('message') or '格式不符合契约'))
            parts.append(f'结构化格式校验：\n{value}')
        if result.get('session_status'):
            parts.append(f"会话状态：\n{_status_text(result['session_status'])}")
        if run.get('error'):
            parts.append(f"运行错误：\n{run['error']}")
        self.result_label.setText('\n\n'.join(parts))
        self.session_button.setVisible(bool(result.get('session_id')))

    def _select_session(self) -> None:
        result = (self._flow.get('run') or {}).get('nodes', {}).get(self._selected, {})
        if result.get('session_id'):
            self.command.emit('select', {'session_id': result['session_id']})

    def add_node(self, pos: QPointF | None = None) -> None:
        if not self._flow or self.running:
            return
        pos = pos or self.canvas.mapToScene(self.canvas.viewport().rect().center())
        row = {'id': uuid.uuid4().hex, 'title': 'AgentCore', 'prompt': '', 'x': pos.x(), 'y': pos.y(),
               'workspace_path': '', 'handoff': '', 'handoff_file': '', 'model_profile': None}
        self._flow.setdefault('nodes', []).append(row)
        item = NodeItem(self, row)
        self.nodes[row['id']] = item
        self.canvas.scene().addItem(item)
        self.canvas.scene().clearSelection()
        item.setSelected(True)
        self._mark_dirty()

    def add_edge(self, source: str, target: str) -> None:
        if self.running or source == target or source not in self.nodes or target not in self.nodes:
            return
        row = {'source': source, 'target': target}
        if row in self._flow.setdefault('edges', []):
            return
        self._flow['edges'].append(row)
        item = EdgeItem(self.nodes[source], self.nodes[target], row)
        self.canvas.scene().addItem(item)
        self.edges.append(item)
        self._mark_dirty()

    def _update_edges(self) -> None:
        for edge in self.edges:
            edge.refresh()

    def delete_selected(self) -> None:
        selected = self.canvas.scene().selectedItems()
        removed = {item.data['id'] for item in selected if isinstance(item, NodeItem)}
        removed_edges = [edge for edge in self.edges if edge in selected or edge.data['source'] in removed or edge.data['target'] in removed]
        if not removed and not removed_edges:
            return
        self._flow['nodes'] = [row for row in self._flow.get('nodes', []) if row['id'] not in removed]
        for edge in removed_edges:
            self._flow['edges'].remove(edge.data)
            self.canvas.scene().removeItem(edge)
            self.edges.remove(edge)
        for node_id in removed:
            self.canvas.scene().removeItem(self.nodes.pop(node_id))
        self._selected = None
        self._mark_dirty()
        self._refresh_state()

    def _submit(self, action: str) -> None:
        if not self._contract_valid():
            return
        if self._pending_project_action or self._directory_update_id:
            return
        if action == 'flow_run' and not self._flow.get('project_id'):
            return
        input_text = self.run_input.toPlainText() if action == 'flow_run' else ''
        if self._project_dirty and self._flow.get('project_id'):
            project = self._flow.get('project') or {}
            changes = {key: project.get(key) for key in ('revision', 'handoff', 'execution_environment', 'permission_policy', 'model_profile')}
            self._pending_project_action = (action, self._flow['id'], self._flow['project_id'], self.draft(), changes, input_text)
            self._refresh_state()
            self.command.emit('project_update', {'project_id': self._flow['project_id'], 'changes': changes})
            return
        self._submit_definition(action, self.draft(), input_text=input_text)

    def _submit_definition(self, action: str, definition: dict, *, input_text: str) -> None:
        definition.pop('project', None)
        self._submitted = (action, copy.deepcopy(definition))
        payload = {'definition': definition}
        if action == 'flow_run':
            payload['flow_id'] = definition.get('id')
            payload['input_text'] = input_text
            self._run_input_submitted = (definition.get('id'), input_text)
        self.command.emit(action, payload)

    def _queue_run(self) -> None:
        if ((self._flow.get('run') or {}).get('status') in ('queued', 'running')
                and self._queued_input is None and any(node.get('reuse_session') for node in self._flow.get('nodes', []))):
            self._queued_input = (self._flow.get('id'), self.run_input.toPlainText())
            self._refresh_state()
            self.command.emit('flow_queue', {'flow_id': self._queued_input[0], 'input_text': self._queued_input[1]})

    def _run_or_stop(self) -> None:
        if self.running:
            self.command.emit('flow_cancel', {'flow_id': self._flow.get('id')})
        elif (self._flow.get('run') or {}).get('status') == 'persistence_failed':
            self.command.emit('flow_retry_completion', {'flow_id': self._flow.get('id')})
        else:
            self._submit('flow_run')

    def accept_result(self, action: str, result: dict) -> None:
        if action == 'flow_queue':
            queued, self._queued_input = self._queued_input, None
            if queued and not result.get('error') and result.get('ok') is not False:
                if self._flow.get('id') == queued[0]:
                    if self.run_input.toPlainText() == queued[1]:
                        self.run_input.clear()
                elif self._run_inputs.get(queued[0]) == queued[1]:
                    self._run_inputs.pop(queued[0], None)
            self._refresh_state()
            return
        if action == 'flow_design':
            self.designer.accept_result(result)
            return
        if action == 'flow_export':
            path, self._export_path = self._export_path, None
            self.export_button.setEnabled(bool(self._flow))
            if path and not result.get('error'):
                data = json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8')
                file = QSaveFile(path)
                if len(data) > 1024 * 1024 or not file.open(QIODevice.OpenModeFlag.WriteOnly) or file.write(data) != len(data) or not file.commit():
                    file.cancelWriting()
                    QMessageBox.warning(self, '导出失败', '无法安全写入模板文件，或模板超过 1 MiB。')
            return
        if action == 'project_update':
            directory_id = self._directory_update_id
            if directory_id and (result.get('error') or result.get('id') == directory_id[1]):
                self._directory_update_id = None
            else:
                directory_id = None
            pending = self._pending_project_action
            project = result.get('project', result)
            if pending and (result.get('error') or project.get('id') == pending[2]):
                self._pending_project_action = None
            else:
                pending = None
            if not result.get('error'):
                active = project.get('id') == self._flow.get('project_id')
                flow_id = pending[1] if pending else directory_id[0] if directory_id else None
                cached = self._cache.get(flow_id) if flow_id and not active else None
                target = self._flow if active else cached[0] if cached else None
                if target is not None:
                    dirty = self._project_dirty if active else self._project_dirty_cache.get(flow_id, False)
                    sent = pending[4] if pending else None
                    changed = dirty and (not sent or any((target.get('project') or {}).get(key) != sent[key] for key in ('handoff', 'execution_environment', 'permission_policy', 'model_profile')))
                    if changed:
                        for key in ('handoff', 'execution_environment', 'permission_policy', 'model_profile'):
                            project[key] = (target.get('project') or {}).get(key)
                    target['project'] = project
                    if active:
                        self._project_dirty = changed
                        self._refresh_state()
                    elif flow_id:
                        self._project_dirty_cache[flow_id] = changed
                if pending:
                    self._submit_definition(pending[0], pending[3], input_text=pending[5])
            self._refresh_state()
            return
        if action in ('flow_delete', 'project_delete') and not result.get('error'):
            identifier = result.get('flow_id')
            self._cache.pop(identifier, None)
            self._project_dirty_cache.pop(identifier, None)
            if self._flow.get('id') == identifier:
                self._flow, self._dirty, self._submitted = {}, False, None
                self._project_dirty = False
                self._rebuild()
                self._refresh_state()
            return
        if action not in ('flow_save', 'flow_run'):
            return
        if result.get('error') or result.get('ok') is False:
            if action == 'flow_run':
                self._run_input_submitted = None
            if self._submitted and self._submitted[0] == action:
                self._submitted = None
            return
        returned = result.get('flow', result)
        if action == 'flow_run' and self._run_input_submitted:
            flow_id, input_text = self._run_input_submitted
            self._run_input_submitted = None
            if self._flow.get('id') == flow_id and self.run_input.toPlainText() == input_text:
                self.run_input.clear()
            elif self._flow.get('id') != flow_id and self._run_inputs.get(flow_id) == input_text:
                self._run_inputs.pop(flow_id, None)
        submitted = None
        own_submission = False
        if (self._submitted and self._submitted[0] == action
                and self._submitted[1].get('id') == returned.get('id')):
            submitted = self._submitted[1]
            self._submitted = None
            own_submission = True
        elif action == 'flow_save':
            # 关闭窗口时父级可按 pending_drafts 逐项保存。
            submitted = copy.deepcopy(returned)
        if not submitted:
            return
        active = self._flow.get('id') == submitted.get('id')
        cached = self._cache.get(submitted.get('id'))
        if not active and not cached:
            return
        target = self._flow if active else cached[0]
        revision = returned.get('revision')
        if revision is not None and target.get('revision') is not None and revision < target['revision']:
            return
        dirty_before = self._dirty if active else cached[1]
        if not own_submission and not dirty_before:
            replacement = copy.deepcopy(returned)
            if active:
                self._flow = replacement
                self._rebuild(preserve_selection=True)
                self._refresh_state()
            else:
                self._cache[submitted['id']] = (replacement, False)
            return
        if revision is not None:
            target['revision'] = revision
        current = copy.deepcopy(target)
        for key in ('run', 'revision', 'project', 'pending_runs', 'session_bindings'):
            current.pop(key, None)
            submitted.pop(key, None)
        dirty = current != submitted or (self._project_dirty if active else self._project_dirty_cache.get(submitted['id'], False))
        if 'run' in returned:
            target['run'] = copy.deepcopy(returned['run'])
        if 'pending_runs' in returned:
            target['pending_runs'] = copy.deepcopy(returned['pending_runs'])
        if active:
            self._dirty = dirty
            self._refresh_state()
        else:
            self._cache[submitted['id']] = (target, dirty)

    def _fit(self) -> None:
        rect = self.canvas.scene().itemsBoundingRect()
        if not rect.isEmpty():
            self.canvas.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)
            scale = self.canvas.transform().m11()
            if scale > 1.0:
                self.canvas.scale(1.0 / scale, 1.0 / scale)
                self.canvas.centerOn(rect.center())

    def _toggle_inspector(self) -> None:
        show = not self.inspector.isVisible()
        self.inspector.setVisible(show)
        if self.width() < 750 and self.designer_button.isChecked():
            self.designer.setVisible(not show)
        self.canvas.setVisible(not (show and self.width() < 750))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.designer.setMaximumWidth(230 if event.size().width() < 750 else 340)
        if event.size().width() < 750 and (event.oldSize().width() <= 0 or event.oldSize().width() >= 750):
            self.inspector.hide()
        if event.size().width() >= 750 or not self.inspector.isVisible():
            self.designer.setVisible(self.designer_button.isChecked())
        self.canvas.setVisible(not (self.inspector.isVisible() and event.size().width() < 750))
