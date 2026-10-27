"""桌面视觉常量与原生矢量绘制，不加载外部图像。"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QComboBox, QLabel, QStyle, QStyledItemDelegate


STYLE = '''
QWidget { background: #1d2026; color: #e0e6ef; font-size: 13px; }
QFrame { border: none; }
QMainWindow, QWidget#conversation { background: #1d2026; }
QWidget#sidebar { background: #15181d; border-right: 1px solid #2b3039; }
QWidget#header { border-bottom: 1px solid #303640; }
QWidget#header QWidget, QWidget#welcome, QWidget#welcome QWidget { background: transparent; }
QLabel { background: transparent; }
QLabel#brand { color: #f0f4fa; font-size: 20px; font-weight: 600; }
QLabel#eyebrow { color: #667587; font-size: 10px; font-weight: 600; }
QLabel#title { color: #edf2fa; font-size: 16px; font-weight: 600; }
QLabel#muted { color: #8b98aa; font-size: 11px; }
QLabel#section { color: #778599; font-size: 11px; padding: 8px 4px 1px; }
QLabel#welcomeTitle { color: #edf2fa; font-size: 26px; font-weight: 600; }
QLabel#welcomeSubtitle { color: #8f9db1; font-size: 13px; }
QLabel#dialogTitle { color: #f0f4fa; font-size: 21px; font-weight: 600; }
QLabel#statusChip { color: #8ed4bb; background: #23342f; border: 1px solid #365148; border-radius: 8px; padding: 3px 9px; font-size: 10px; }
QLabel#statusChip[tone="running"] { color: #aac8ff; background: #263448; border-color: #3d567a; }
QLabel#statusChip[tone="warning"] { color: #e8c382; background: #3d3324; border-color: #655239; }
QLabel#statusChip[tone="error"] { color: #e8a5ad; background: #3b282e; border-color: #63414c; }
QLabel#error { color: #efb2bb; padding: 10px 13px; background: #35272f; border: 1px solid #603d4a; border-radius: 9px; }
QLineEdit, QComboBox, QTextEdit { background: #242932; border: 1px solid #3a424e; border-radius: 8px; padding: 9px 11px; selection-background-color: #3b577b; selection-color: #f5f8ff; }
QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border-color: #7d9fc9; }
QLineEdit:disabled, QComboBox:disabled { color: #657184; background: #20242b; border-color: #303640; }
QLineEdit#search { background: #1b1f26; border-color: #2b323d; padding: 8px; }
QComboBox { min-height: 18px; padding-right: 26px; }
QComboBox::drop-down { width: 24px; border: none; }
QComboBox::down-arrow { image: none; width: 0; height: 0; }
QComboBox QAbstractItemView { background: #252c37; border: 1px solid #47566b; selection-background-color: #354b68; padding: 5px; }
QTextBrowser { background: transparent; border: none; padding: 0; selection-background-color: #3b577b; selection-color: #f5f8ff; }
QTextEdit#composer { background: transparent; border: none; border-radius: 0; padding: 3px; font-size: 14px; }
QWidget#composerPanel { background: #252b34; border: 1px solid #414b5a; border-radius: 14px; }
QWidget#composerPanel[focused="true"] { border-color: #769bc6; }
QWidget#composerPanel QLabel { background: transparent; border: none; }
QWidget#composerPanel QPushButton { border-radius: 8px; }
QPushButton { color: #c8d3e2; border: 1px solid #3b4451; background: #2b323d; border-radius: 8px; padding: 8px 12px; }
QPushButton:hover { color: #f3f6fc; background: #343e4c; border-color: #56677d; }
QPushButton:pressed { background: #232e3e; }
QPushButton:focus { border-color: #7d9fc9; }
QPushButton:disabled { color: #657184; border-color: #303742; background: #242a33; }
QPushButton#primary { background: #a5c4f4; color: #17263c; border: 1px solid #a5c4f4; font-weight: 600; }
QPushButton#primary:hover { background: #bdd6fc; border-color: #bdd6fc; }
QPushButton#primary:disabled { color: #60728c; background: #344359; border-color: #344359; }
QPushButton#newSession { text-align: left; background: #263344; color: #d4e6ff; border-color: #40546f; padding: 10px 12px; }
QPushButton#newSession:hover { background: #30435b; border-color: #6d8cb4; }
QPushButton#iconButton { border: none; background: transparent; padding: 7px; }
QPushButton#iconButton:hover { background: #303944; }
QPushButton#quiet { color: #8f9db1; border: none; background: transparent; text-align: left; padding: 9px 10px; }
QPushButton#quiet:hover { background: #242c37; color: #d3deed; }
QPushButton#suggestion { text-align: left; color: #c3d1e2; background: #222832; border-color: #353e4c; padding: 13px 17px; font-size: 12px; }
QPushButton#suggestion:hover { background: #293443; border-color: #637e9f; }
QPushButton#history { color: #8fa4be; background: transparent; border: none; padding: 4px 8px; font-size: 11px; }
QPushButton#history:hover { color: #c8e0ff; background: #293544; }
QPushButton#stop { color: #eab4bf; background: #422e37; border-color: #65414e; }
QListWidget { background: transparent; border: none; outline: none; padding: 0; }
QListWidget::item { padding: 10px; margin: 2px 0; border-radius: 8px; }
QListWidget::item:selected { background: #2b3d54; color: #eaf2ff; }
QListWidget::item:hover { background: #252e3b; }
QTreeView { background: #20252d; border: 1px solid #303a48; outline: none; }
QTreeView:focus { border-color: #7d9fc9; }
QTreeView::item:selected { background: #2b3d54; color: #eaf2ff; }
QHeaderView::section { background: #252c36; color: #a8b7ca; border: none; border-bottom: 1px solid #303a48; padding: 6px; }
QDockWidget { border-left: 1px solid #303a48; font-weight: 600; }
QDockWidget::title { padding: 12px; background: #20262f; text-align: left; }
QTabWidget::pane { border: none; border-top: 1px solid #323c4b; }
QTabBar::tab { color: #8494aa; background: transparent; padding: 10px 16px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #bed6fa; border-bottom-color: #90b5eb; }
QTabBar::tab:hover { color: #d3e2f6; }
QCheckBox { color: #96a5b9; font-size: 11px; spacing: 8px; padding: 5px 0; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #586b84; border-radius: 4px; background: #222a35; }
QCheckBox::indicator:checked { background: #8bb5ed; border-color: #a8ccfa; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QSplitter::handle { background: #303640; width: 1px; height: 1px; }
QSplitter::handle:hover { background: #617b9c; }
QStatusBar { color: #69798e; font-size: 10px; background: #191d23; border-top: 1px solid #2b323c; padding: 2px 10px; }
QStatusBar::item { border: none; }
QToolTip { background: #303c4d; color: #edf3fc; border: 1px solid #586f8c; padding: 6px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 3px 0; }
QScrollBar::handle:vertical { background: #455166; min-height: 28px; border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: #687e9b; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 8px; }
QScrollBar::handle:horizontal { background: #455166; min-width: 28px; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
'''


@lru_cache(maxsize=96)
def icon(name: str, color: str = '#9badc5', size: int = 20) -> QIcon:
    """使用统一线宽绘制小图标；禁用状态同样由本地矢量生成。"""
    result = QIcon()
    for mode, shade in [(QIcon.Mode.Normal, color), (QIcon.Mode.Disabled, '#59687c')]:
        pixmap = QPixmap(size * 2, size * 2)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 24, size / 24)
        painter.setPen(QPen(QColor(shade), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))

        def line(*points: tuple[float, float]) -> None:
            path = QPainterPath(QPointF(*points[0]))
            for point in points[1:]:
                path.lineTo(QPointF(*point))
            painter.drawPath(path)

        if name == 'brand':
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(shade))
            for x, y, w, h in [(4, 4, 7, 7), (13, 4, 7, 7), (4, 13, 7, 7), (13, 13, 7, 7)]:
                painter.drawRoundedRect(QRectF(x, y, w, h), 2.2, 2.2)
        elif name == 'plus':
            line((12, 5), (12, 19))
            line((5, 12), (19, 12))
        elif name == 'menu':
            painter.drawRoundedRect(QRectF(4, 5, 16, 14), 2, 2)
            line((10, 5), (10, 19))
        elif name == 'search':
            painter.drawEllipse(QRectF(4, 4, 11, 11))
            line((14, 14), (20, 20))
        elif name == 'settings':
            for y, x in [(6, 9), (12, 16), (18, 11)]:
                line((4, y), (x - 2, y))
                line((x + 2, y), (20, y))
                painter.drawEllipse(QRectF(x - 2, y - 2, 4, 4))
        elif name == 'activity':
            line((3, 13), (7, 13), (10, 6), (14, 19), (17, 11), (21, 11))
        elif name in {'folder', 'up'}:
            line((3, 7), (3, 18), (21, 18), (21, 7), (11, 7), (9, 5), (3, 5), (3, 7))
            if name == 'up':
                line((12, 15), (12, 10))
                line((9, 12), (12, 9), (15, 12))
        elif name == 'file':
            line((6, 3), (14, 3), (19, 8), (19, 21), (6, 21), (6, 3))
            line((14, 3), (14, 8), (19, 8))
            line((9, 13), (16, 13))
            line((9, 17), (15, 17))
        elif name == 'send':
            line((12, 19), (12, 5))
            line((6, 11), (12, 5), (18, 11))
        elif name == 'stop':
            painter.drawRoundedRect(QRectF(6, 6, 12, 12), 2, 2)
        elif name == 'trash':
            line((4, 7), (20, 7))
            line((9, 4), (15, 4))
            line((7, 7), (8, 20), (16, 20), (17, 7))
            line((10, 10), (10.5, 17))
            line((14, 10), (13.5, 17))
        elif name == 'model':
            line((12, 3), (21, 8), (12, 13), (3, 8), (12, 3))
            line((3, 12), (12, 17), (21, 12))
            line((3, 16), (12, 21), (21, 16))
        elif name == 'refresh':
            painter.drawArc(QRectF(5, 5, 14, 14), 40 * 16, 275 * 16)
            line((16, 4), (20, 5), (19, 9))
        elif name == 'branch':
            line((6, 4), (6, 14), (17, 14))
            line((13, 10), (17, 14), (13, 18))
        elif name == 'code':
            line((8, 6), (3, 12), (8, 18))
            line((16, 6), (21, 12), (16, 18))
            line((14, 4), (10, 20))
        elif name == 'chat':
            line((5, 5), (19, 5), (20, 6), (20, 15), (19, 16),
                 (11, 16), (7, 19), (7, 16), (5, 16), (4, 15), (4, 6), (5, 5))
        else:
            painter.drawRoundedRect(QRectF(4, 5, 16, 12), 3, 3)
            line((8, 17), (8, 21), (12, 17))
        painter.end()
        result.addPixmap(pixmap, mode)
    return result


class ElidingLabel(QLabel):
    """仅在绘制时省略，保留完整 text() 与无障碍文本。"""

    def minimumSizeHint(self) -> QSize:
        return QSize(12, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), self.alignment() | Qt.AlignmentFlag.AlignVCenter, text)


class ChoiceBox(QComboBox):
    """深色表单的下拉指示由原生绘制，避免平台默认黑色箭头。"""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#9badc5' if self.isEnabled() else '#59687c'), 1.5,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        x, y = self.width() - 16, self.height() / 2
        path = QPainterPath(QPointF(x - 4, y - 2))
        path.lineTo(QPointF(x, y + 2))
        path.lineTo(QPointF(x + 4, y - 2))
        painter.drawPath(path)


class SessionDelegate(QStyledItemDelegate):
    """会话名称与状态分层绘制，树层级只影响展示，不改变选择模型。"""

    def sizeHint(self, option, index) -> QSize:
        return QSize(180, 64)

    def paint(self, painter: QPainter, option, index) -> None:
        data = index.data(Qt.ItemDataRole.UserRole + 1) or {}
        title, status = data.get('title', ''), data.get('status', '')
        depth = min(data.get('depth', 0), 3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        box = QRectF(option.rect).adjusted(2 + depth * 10, 3, -3, -3)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#405776' if selected else '#252c37' if hovered else '#15181d'), 1))
        painter.setBrush(QColor('#25354b' if selected else '#202833' if hovered else '#15181d'))
        painter.drawRoundedRect(box, 9, 9)
        x, y = box.x() + 11, box.y() + 11
        icon('branch' if depth else 'chat', '#8db6ec' if selected else '#74859d', 17).paint(painter, int(x), int(y + 1), 17, 17)
        title_box = QRectF(x + 27, y - 1, box.width() - 50, 21)
        font = option.font
        font.setPixelSize(13)
        font.setWeight(font.Weight.Medium if selected else font.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor('#e3edfc' if selected else '#c0ccdd'))
        painter.drawText(title_box, Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, int(title_box.width())))
        shade = '#7ad2b0' if status in {'running', 'idle', 'checkpoint'} else '#dcaf6b' if status in {'confirming', 'queued'} else '#d88d9c' if status in {'error', 'interrupted'} else '#718197'
        painter.setBrush(QColor(shade))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(x + 28, y + 29, 5, 5))
        font.setPixelSize(10)
        font.setWeight(font.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor('#94a6be' if selected else '#73849b'))
        painter.drawText(QRectF(x + 40, y + 22, box.width() - 61, 20), Qt.AlignmentFlag.AlignVCenter, data.get('status_label', status))
        painter.restore()
