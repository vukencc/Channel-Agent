"""原生 Agent Flow 画布与本地草稿。"""
from __future__ import annotations

import copy
import math
import uuid

from PySide6.QtCore import QMimeData, QPointF, QRectF, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGraphicsItem, QGraphicsPathItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .theme import ChoiceBox


STATUS_LABELS = {
    'pending': '待运行', 'queued': '排队中', 'running': '运行中', 'completed': '已完成',
    'failed': '失败', 'cancelled': '已取消', 'cancelling': '正在取消', 'interrupted': '已中断',
    'confirming': '等待确认', 'idle': '空闲', 'checkpoint': '检查点',
}


def _status_text(status) -> str:
    return STATUS_LABELS.get(status, str(status)) if status else '未运行'


def _status_color(status) -> str:
    if status in ('completed',):
        return '#7ad2b0'
    if status in ('failed', 'interrupted', 'error'):
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
        painter.drawRoundedRect(QRectF(0, 0, 200, 90), 10, 10)
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
        self.nodes, self.edges = {}, []
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        header = QHBoxLayout()
        self.heading, self.status = _label('Agent Flow'), _label('')
        self.heading.setObjectName('title')
        header.addWidget(self.heading, 1)
        header.addWidget(self.status)
        root.addLayout(header)
        controls = QHBoxLayout()
        self.add_button = AddNodeButton('添加 AgentCore')
        self.save_button = QPushButton('保存')
        self.run_button = QPushButton('运行')
        self.fit_button = QPushButton('适配')
        self.inspector_button = QPushButton('检查器')
        for button in (self.add_button, self.save_button, self.run_button, self.fit_button, self.inspector_button):
            controls.addWidget(button)
        root.addLayout(controls)
        body = QHBoxLayout()
        root.addLayout(body, 1)
        self.canvas = FlowCanvas(self)
        body.addWidget(self.canvas, 1)
        self.inspector = QTabWidget()
        self.inspector.setMinimumWidth(270)
        self.inspector.setMaximumWidth(340)
        body.addWidget(self.inspector)
        self.flow_fields = self._make_form([
            ('名称', 'title', 'line'), ('项目目录', 'workspace_path', 'line'),
            ('初始 Handoff', 'handoff', 'text'), ('执行环境', 'execution_environment', 'environment'),
            ('权限模式', 'permission_policy', 'policy'), ('模型', 'model_profile', 'model')])
        self.node_fields = self._make_form([
            ('名称', 'title', 'line'), ('初始提示', 'prompt', 'text'),
            ('项目目录（留空继承）', 'workspace_path', 'line'), ('Handoff', 'handoff', 'text'),
            ('Handoff 文件相对路径', 'handoff_file', 'line'), ('模型', 'model_profile', 'model')])
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
        fields = {}
        is_node = hasattr(self, 'flow_fields')
        for caption, key, kind in rows:
            if kind == 'text':
                field = QTextEdit()
                field.setAcceptRichText(False)
                field.setMinimumHeight(90)
                if key == 'handoff':
                    field.setMaximumHeight(130)
                field.textChanged.connect(lambda k=key, f=field, n=is_node: self._field_changed(k, f.toPlainText(), n))
            elif kind == 'line':
                field = QLineEdit()
                field.textChanged.connect(lambda value, k=key, n=is_node: self._field_changed(k, value, n))
            else:
                field = ChoiceBox()
                choices = [('继承', None)]
                if kind == 'environment':
                    choices += [('隔离沙箱', 'sandbox'), ('Linux 本机', 'local')]
                elif kind == 'policy':
                    choices += [('智能申请', 'smart'), ('只读', 'readonly'), ('完全访问', 'full_access')]
                for label, value in choices:
                    field.addItem(label, value)
                field.currentIndexChanged.connect(lambda _, k=key, f=field, n=is_node: self._field_changed(k, f.currentData(), n))
            fields[key] = field
            form.addRow(_label(caption), field)
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
        return copy.deepcopy(self._flow)

    def pending_drafts(self) -> list[dict]:
        """返回尚未保存的所有 Flow 草稿，不暴露内部对象。"""
        drafts = {key: value for key, (value, dirty) in self._cache.items() if dirty}
        current_id = self._flow.get('id')
        if current_id:
            drafts.pop(current_id, None)
            if self._dirty:
                drafts[current_id] = self._flow
        return copy.deepcopy(list(drafts.values()))

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._refresh_header()

    def _field_changed(self, key, value, node) -> None:
        if self._loading or self.running or not self._flow:
            return
        target = self.nodes.get(self._selected).data if node and self._selected in self.nodes else (None if node else self._flow)
        if target is not None and target.get(key) != value:
            target[key] = value
            self._mark_dirty()
            if node:
                self.nodes[self._selected].update()

    def _fill_fields(self, fields, data) -> None:
        for key, field in fields.items():
            value = data.get(key)
            if isinstance(field, QComboBox):
                index = field.findData(value)
                field.setCurrentIndex(max(0, index))
            elif isinstance(field, QTextEdit):
                field.setPlainText(str(value or ''))
            else:
                field.setText(str(value or ''))

    def set_flow(self, flow: dict | None, models: list[dict] | None = None) -> None:
        incoming = copy.deepcopy(flow or {})
        old_id, new_id = self._flow.get('id'), incoming.get('id')
        if models is not None and models != self._models:
            self._models = copy.deepcopy(models)
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
                self._cache[old_id] = (copy.deepcopy(self._flow), self._dirty)
            cached = self._cache.get(new_id)
            if cached and cached[1]:
                self._flow, self._dirty = copy.deepcopy(cached[0]), True
                self._flow['run'] = incoming.get('run')
            else:
                self._flow, self._dirty = incoming, False
            self._rebuild()
        elif (not self._dirty and incoming.get('revision') is not None
              and (self._flow.get('revision') is None or incoming['revision'] > self._flow['revision'])):
            self._flow = incoming
            self._rebuild(preserve_selection=True)
        else:
            self._flow['run'] = incoming.get('run')
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
                self._fill_fields(self.flow_fields, self._flow)
                self._fill_fields(self.node_fields, selected.data if selected else {})
        finally:
            self._loading = False

    def _refresh_header(self) -> None:
        self.heading.setText(str(self._flow.get('title') or 'Agent Flow') + (' *' if self._dirty else ''))
        self.status.setText(_status_text((self._flow.get('run') or {}).get('status')))

    def _refresh_state(self) -> None:
        self._refresh_header()
        running = self.running
        self.add_button.setEnabled(bool(self._flow) and not running)
        self.save_button.setEnabled(bool(self._flow) and not running)
        self.run_button.setEnabled(bool(self._flow))
        self.run_button.setText('停止' if running else '运行')
        for item in self.nodes.values():
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not running)
            item.update()
        for fields, enabled in ((self.flow_fields, not running), (self.node_fields, not running and bool(self._selected))):
            for field in fields.values():
                if isinstance(field, (QTextEdit, QLineEdit)):
                    field.setReadOnly(not enabled)
                else:
                    field.setEnabled(enabled)
        self._show_result()

    def _selection_changed(self) -> None:
        if self._loading:
            return
        selected = next((item for item in self.canvas.scene().selectedItems() if isinstance(item, NodeItem)), None)
        self._selected = selected.data['id'] if selected else None
        self._loading = True
        self._fill_fields(self.node_fields, selected.data if selected else {})
        self._loading = False
        if selected:
            results = (self._flow.get('run') or {}).get('nodes', {})
            self.inspector.setCurrentIndex(2 if self._selected in results else 1)
        self._refresh_state()

    def _show_result(self) -> None:
        run = self._flow.get('run') or {}
        result = run.get('nodes', {}).get(self._selected, {})
        parts = []
        for title, key in [('状态', 'status'), ('项目目录', 'workspace_path'), ('输入 Handoff', 'received_handoff'), ('输出 Handoff', 'output_handoff'), ('错误', 'error')]:
            if result.get(key):
                value = _status_text(result[key]) if key == 'status' else result[key]
                parts.append(f"{title}：\n{value}")
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
        definition = self.draft()
        self._submitted = (action, copy.deepcopy(definition))
        payload = {'definition': definition}
        if action == 'flow_run':
            payload['flow_id'] = self._flow.get('id')
        self.command.emit(action, payload)

    def _run_or_stop(self) -> None:
        if self.running:
            self.command.emit('flow_cancel', {'flow_id': self._flow.get('id')})
        else:
            self._submit('flow_run')

    def accept_result(self, action: str, result: dict) -> None:
        if action == 'flow_delete' and not result.get('error'):
            identifier = result.get('flow_id')
            self._cache.pop(identifier, None)
            if self._flow.get('id') == identifier:
                self._flow, self._dirty, self._submitted = {}, False, None
                self._rebuild()
                self._refresh_state()
            return
        if action not in ('flow_save', 'flow_run'):
            return
        if result.get('error') or result.get('ok') is False:
            if self._submitted and self._submitted[0] == action:
                self._submitted = None
            return
        returned = result.get('flow', result)
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
        for key in ('run', 'revision'):
            current.pop(key, None)
            submitted.pop(key, None)
        dirty = current != submitted
        if 'run' in returned:
            target['run'] = copy.deepcopy(returned['run'])
        if active:
            self._dirty = dirty
            self._refresh_state()
        else:
            self._cache[submitted['id']] = (target, dirty)

    def _fit(self) -> None:
        rect = self.canvas.scene().itemsBoundingRect()
        if not rect.isEmpty():
            self.canvas.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)

    def _toggle_inspector(self) -> None:
        show = not self.inspector.isVisible()
        self.inspector.setVisible(show)
        self.canvas.setVisible(not (show and self.width() < 750))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if event.oldSize().width() <= 0 and event.size().width() < 750:
            self.inspector.hide()
        self.canvas.setVisible(not (self.inspector.isVisible() and event.size().width() < 750))
