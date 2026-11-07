"""原生 Flow 画布适配倍率回归。"""
import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtWidgets import QApplication

from agent_frontend.desktop.flow import FlowPanel


def test_fit_caps_enlargement_and_shrinks_large_flow():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.resize(1200, 800)
    panel.show()
    for count in (1, 2):
        nodes = [{'id': str(i), 'title': '节点', 'x': i * 230, 'y': 0} for i in range(count)]
        panel.set_flow({'id': str(count), 'nodes': nodes, 'edges': []})
        app.processEvents()
        panel._fit()
        assert panel.canvas.transform().m11() <= 1.0 + 1e-9
    panel.resize(700, 800)
    nodes = [{'id': str(i), 'title': '节点', 'x': i * 300, 'y': 0} for i in range(4)]
    panel.set_flow({'id': 'many', 'nodes': nodes, 'edges': []})
    app.processEvents()
    panel._fit()
    assert 0 < panel.canvas.transform().m11() < 1.0
    bounds = panel.canvas.mapFromScene(panel.canvas.scene().itemsBoundingRect()).boundingRect()
    assert panel.canvas.viewport().rect().contains(bounds)
    panel.close()
    panel.deleteLater()
