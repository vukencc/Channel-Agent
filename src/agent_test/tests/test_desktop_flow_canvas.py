import os
import sys
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from agent_frontend.desktop.flow import FlowPanel


def test_delete_discards_only_deleted_flow_drafts():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.set_flow({'id': 'deleted', 'title': '删除', 'revision': 0, 'nodes': [], 'edges': []})
    panel.add_node()
    panel.set_flow({'id': 'retained', 'title': '保留', 'revision': 0, 'nodes': [], 'edges': []})
    panel.add_node()
    panel.accept_result('flow_delete', {'flow_id': 'deleted'})
    assert {flow['id'] for flow in panel.pending_drafts()} == {'retained'}
    panel.accept_result('flow_delete', {'flow_id': 'retained'})
    panel.set_flow(None)
    assert panel.pending_drafts() == []
    panel.close()


def test_flow_draft_snapshot_and_run_lock():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.resize(500, 600)
    panel.show()
    flow = {'id': 'f', 'title': 'Flow', 'revision': 1, 'nodes': [], 'edges': []}
    panel.set_flow(flow)
    panel.add_node()
    panel.prompt_edit.setPlainText('draft')
    assert panel.is_dirty
    panel.set_flow({**flow, 'run': {'status': 'running', 'nodes': {}}})
    assert panel.draft()['nodes'][0]['prompt'] == 'draft'
    assert panel.prompt_edit.isReadOnly()
    assert not panel.inspector.isVisible()
    panel.close()


def test_mouse_drag_nodes_connect_and_submit_draft():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.resize(1000, 700)
    panel.show()
    panel.set_flow({'id': 'f', 'title': 'Flow', 'revision': 1, 'nodes': [], 'edges': []})
    panel.add_node(QPointF(-260, -100))
    panel.add_node(QPointF(100, -100))
    app.processEvents()
    first, second = list(panel.nodes.values())
    viewport = panel.canvas.viewport()
    start = panel.canvas.mapFromScene(first.mapToScene(QPointF(200, 45)))
    end = panel.canvas.mapFromScene(second.mapToScene(QPointF(0, 45)))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(viewport, end, 20)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    assert panel.draft()['edges'] == [{'source': first.data['id'], 'target': second.data['id']}]
    old = first.pos()
    start = panel.canvas.mapFromScene(first.mapToScene(QPointF(80, 30)))
    end = start + panel.canvas.mapFromScene(QPointF(60, 40)) - panel.canvas.mapFromScene(QPointF(0, 0))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(viewport, end, 20)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    assert first.pos() != old
    sent = []
    panel.command.connect(lambda action, payload: sent.append((action, payload)))
    panel.run_button.click()
    assert sent[-1][0] == 'flow_run'
    assert sent[-1][1]['definition']['edges'] == panel.draft()['edges']
    panel.close()


def test_save_revision_never_rolls_back_and_narrow_inspector():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.resize(500, 600)
    panel.show()
    flow = {'id': 'f', 'title': 'Flow', 'revision': 1, 'nodes': [], 'edges': []}
    panel.set_flow(flow)
    panel.add_node()
    panel.prompt_edit.setPlainText('<img src="file:///private">')
    panel.save_button.click()
    saved = panel.draft()
    saved['revision'] = 2
    panel.accept_result('flow_save', saved)
    assert not panel.is_dirty
    panel.set_flow(flow)
    assert panel.draft()['revision'] == 2
    assert panel.draft()['nodes'][0]['prompt'] == '<img src="file:///private">'
    panel.inspector_button.click()
    assert panel.inspector.isVisible()
    assert not panel.canvas.isVisible()
    panel.inspector_button.click()
    assert panel.canvas.isVisible()
    panel.close()


def test_run_details_are_plain_text_and_session_selection():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.resize(1000, 700)
    panel.show()
    node = {'id': 'n', 'title': 'Agent', 'prompt': 'task', 'x': 0, 'y': 0}
    flow = {'id': 'f', 'title': 'Flow', 'revision': 1, 'nodes': [node], 'edges': []}
    panel.set_flow(flow)
    panel.nodes['n'].setSelected(True)
    sent = []
    panel.command.connect(lambda action, payload: sent.append((action, payload)))
    flow['run'] = {'status': 'running', 'nodes': {'n': {
        'status': 'running', 'session_id': 'session', 'received_handoff': '<img src="file:///private">',
        'output_handoff': 'output', 'error': 'failure'}}}
    panel.set_flow(flow)
    assert panel.result_label.textFormat() == Qt.TextFormat.PlainText
    assert '<img src="file:///private">' in panel.result_label.text()
    assert '运行中' in panel.result_label.text()
    assert panel.draft()['run']['nodes']['n']['status'] == 'running'
    panel.nodes['n'].setSelected(False)
    panel.nodes['n'].setSelected(True)
    assert panel.inspector.tabText(panel.inspector.currentIndex()) == '运行'
    assert panel.prompt_edit.isReadOnly()
    assert not panel.nodes['n'].flags() & panel.nodes['n'].GraphicsItemFlag.ItemIsMovable
    panel.session_button.click()
    assert sent[-1] == ('select', {'session_id': 'session'})
    panel.close()


def test_revision_rebuild_preserves_selection_without_qt_slot_exception(monkeypatch):
    app = QApplication.instance() or QApplication([])
    errors = []
    monkeypatch.setattr(sys, 'excepthook', lambda *args: errors.append(args))
    panel = FlowPanel()
    panel.resize(1000, 700)
    panel.show()
    node = {'id': 'n', 'title': 'Agent', 'prompt': 'task', 'x': 0, 'y': 0}
    flow = {'id': 'f', 'title': 'Flow', 'revision': 1, 'nodes': [node], 'edges': []}
    panel.set_flow(flow)
    panel.nodes['n'].setSelected(True)
    panel.canvas.scale(1.2, 1.2)
    transform = panel.canvas.transform()
    panel.set_flow({**flow, 'revision': 2})
    app.processEvents()
    assert not errors
    assert panel.nodes['n'].isSelected()
    assert panel.prompt_edit.toPlainText() == 'task'
    assert panel.canvas.transform() == transform
    panel.close()


def test_pending_drafts_include_cached_and_accept_external_save():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    a = {'id': 'a', 'title': 'A', 'revision': 1, 'nodes': [], 'edges': []}
    b = {'id': 'b', 'title': 'B', 'revision': 1, 'nodes': [], 'edges': []}
    panel.set_flow(a)
    panel.flow_fields['title'].setText('A changed')
    panel.set_flow(b)
    panel.flow_fields['title'].setText('B changed')
    drafts = panel.pending_drafts()
    assert {row['id'] for row in drafts} == {'a', 'b'}
    drafts[0]['title'] = 'external mutation'
    assert 'external mutation' not in [row['title'] for row in panel.pending_drafts()]
    saved = next(row for row in panel.pending_drafts() if row['id'] == 'a')
    saved['revision'] = 2
    panel.accept_result('flow_save', saved)
    assert [row['id'] for row in panel.pending_drafts()] == ['b']
    panel.close()


def test_external_save_populates_clean_flow_and_ignores_older_ack():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    empty = {'id': 'f', 'title': 'Flow', 'revision': 0, 'nodes': [], 'edges': []}
    nodes = [{'id': str(i), 'title': 'Agent', 'prompt': 'task', 'x': i * 220, 'y': 0}
             for i in range(3)]
    backend = {**empty, 'revision': 1, 'nodes': nodes}
    panel.set_flow(empty)
    panel.accept_result('flow_save', backend)
    panel.set_flow(backend)
    assert len(panel.nodes) == 3
    assert not panel.is_dirty
    panel.accept_result('flow_save', empty)
    assert panel.draft()['revision'] == 1
    assert len(panel.nodes) == 3
    panel.close()
