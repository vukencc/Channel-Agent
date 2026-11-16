"""首次选择画布的真实 Qt 几何与后续视图保持。"""
import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget
from agent_frontend.desktop.flow import FlowPanel


@pytest.mark.parametrize('width,designer', [(1242, False), (1242, True), (700, True)])
def test_new_identity_fits_once_and_refresh_preserves_view(width, designer):
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.resize(width, 872)
    panel.show()
    panel.designer_button.setChecked(designer)
    panel.set_flow({'id': 'empty', 'revision': 1, 'nodes': [], 'edges': []})
    app.processEvents()
    panel.canvas.centerOn(-472, -342)
    nodes = [{'id': str(i), 'title': '节点', 'x': i * 310, 'y': (i % 2) * 200} for i in range(5)]
    edges = [{'id': str(i), 'source': str(i), 'target': str(i + 1)} for i in range(4)]
    flow = {'id': 'generated', 'revision': 1, 'nodes': nodes, 'edges': edges}
    panel.set_flow(flow)
    app.processEvents()
    app.processEvents()
    bounds = panel.canvas.mapFromScene(panel.canvas.scene().itemsBoundingRect()).boundingRect()
    assert panel.canvas.viewport().rect().contains(bounds)
    panel.canvas.scale(1.3, 1.3)
    panel.canvas.centerOn(700, 200)
    transform = panel.canvas.transform()
    center = panel.canvas.mapToScene(panel.canvas.viewport().rect().center())
    panel.set_flow({**flow, 'run': {'status': 'running', 'nodes': {}}})
    app.processEvents()
    assert panel.canvas.transform() == transform
    assert panel.canvas.mapToScene(panel.canvas.viewport().rect().center()) == center
    panel.set_flow({**flow, 'revision': 2})
    app.processEvents()
    assert panel.canvas.transform() == transform
    assert panel.canvas.mapToScene(panel.canvas.viewport().rect().center()) == center
    panel.close()


def test_hidden_panel_fits_current_identity_when_shown():
    app = QApplication.instance() or QApplication([])
    stack = QStackedWidget()
    stack.addWidget(QWidget())
    panel = FlowPanel()
    stack.addWidget(panel)
    stack.resize(900, 800)
    stack.show()
    panel.set_flow({'id': 'superseded', 'nodes': [{'id': 'old', 'x': -1000, 'y': -500}], 'edges': []})
    nodes = [{'id': str(i), 'x': i * 300, 'y': 0} for i in range(5)]
    panel.set_flow({'id': 'current', 'nodes': nodes, 'edges': []})
    app.processEvents()
    stack.setCurrentWidget(panel)
    app.processEvents()
    app.processEvents()
    bounds = panel.canvas.mapFromScene(panel.canvas.scene().itemsBoundingRect()).boundingRect()
    assert panel.canvas.viewport().rect().contains(bounds)
    stack.close()
