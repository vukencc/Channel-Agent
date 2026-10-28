"""原生主题的离屏渲染检查。"""
import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtWidgets import QApplication, QTreeView

from agent_frontend.desktop.theme import STYLE


def test_tree_view_uses_theme_border() -> None:
    app = QApplication.instance() or QApplication([])
    view = QTreeView()
    view.setStyleSheet(STYLE)
    view.resize(200, 100)
    view.show()
    app.processEvents()
    view.clearFocus()
    app.processEvents()
    assert view.grab().toImage().pixelColor(0, 20).name() == "#303a48"
    view.setFocus()
    app.processEvents()
    assert view.grab().toImage().pixelColor(0, 20).name() == "#7d9fc9"
    view.close()
