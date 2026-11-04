"""原生 Flow 编辑器的节点设置回归。"""
import asyncio
import json
from types import SimpleNamespace
import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtWidgets import QApplication, QBoxLayout, QFileDialog, QMessageBox
from agent_frontend.desktop.flow import FlowPanel
from agent_frontend.desktop.runtime import RuntimeBridge
from agent_frontend.desktop.window import MainWindow


def test_node_authoring_settings_survive_draft_switch():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    flow = {'id': 'one', 'title': 'One', 'revision': 1, 'nodes': [], 'edges': []}
    panel.set_flow(flow, [])
    panel.add_node()
    panel.node_fields['reasoning_effort'].setCurrentIndex(
        panel.node_fields['reasoning_effort'].findData('high'))
    panel.node_fields['thinking_mode'].setCurrentIndex(
        panel.node_fields['thinking_mode'].findData('enabled'))
    panel.set_flow({'id': 'two', 'title': 'Two', 'revision': 1, 'nodes': [], 'edges': []}, [])
    panel.set_flow(flow, [])
    assert panel.draft()['nodes'][0]['reasoning_effort'] == 'high'
    assert panel.draft()['nodes'][0]['thinking_mode'] == 'enabled'
    panel.deleteLater()


def test_tool_selection_is_locked_and_preserved():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.set_tool_catalog({'tools': [
        {'name': 'read_file', 'required': True},
        {'name': 'search', 'required': False},
    ]})
    panel.set_flow({'id': 'one', 'revision': 1, 'nodes': [], 'edges': []}, [])
    panel.add_node()
    panel.tools_mode.setCurrentIndex(1)
    assert not panel.tool_checks['read_file'].isEnabled()
    panel.tool_checks['search'].setChecked(True)
    assert set(panel.draft()['nodes'][0]['tool_names']) == {'read_file', 'search'}
    panel.set_flow({'id': 'one', 'revision': 1, 'nodes': [], 'edges': [], 'run': {'status': 'running'}}, [])
    assert not panel.tools_mode.isEnabled()
    assert not panel.tool_checks['search'].isEnabled()
    panel.deleteLater()


def test_cancelled_design_cannot_import_late_result(tmp_path, monkeypatch):
    asyncio.run(_cancelled_design_cannot_import_late_result(tmp_path, monkeypatch))


async def _cancelled_design_cannot_import_late_result(tmp_path, monkeypatch):
    import agent_core.core.flow_designer as designer
    started = asyncio.Event()
    release = asyncio.Event()
    imported = []

    async def late(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        return {'reply': 'late', 'template': {'format': 'agent-flow'}}

    monkeypatch.setattr(designer, 'design_flow', late)
    bridge = RuntimeBridge(tmp_path)
    bridge._mutations = asyncio.Lock()
    bridge._flows = SimpleNamespace(import_template=lambda payload: imported.append(payload))
    bridge._schedule_command('flow_design', {'messages': [], 'request_id': 'old'})
    await started.wait()
    bridge._schedule_command('flow_design_cancel', {})
    await asyncio.gather(*list(bridge._commands), return_exceptions=True)
    assert imported == []


def test_designer_rejects_old_response():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    designer = panel.designer
    designer.input.setText('设计一个流程')
    designer.submit()
    old = designer.request_id
    designer.cancel()
    designer.input.setText('另一个流程')
    designer.submit()
    designer.accept_result({'request_id': old, 'reply': '过期'})
    assert designer.busy
    assert '过期' not in designer.transcript.toPlainText()
    panel.deleteLater()


def test_file_dialog_import_and_atomic_draft_export(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.set_flow({'id': 'draft', 'revision': 1, 'nodes': [], 'edges': [], 'title': '草稿'}, [])
    received = []
    panel.command.connect(lambda action, payload: received.append((action, payload)))
    path = tmp_path / 'flow.json'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (str(path), ''))
    panel._export_file()
    assert received[-1][0] == 'flow_export'
    assert received[-1][1]['definition']['title'] == '草稿'
    portable = {'format': 'agent-flow', 'version': 1, 'title': '草稿', 'nodes': [], 'edges': []}
    panel.accept_result('flow_export', portable)
    assert json.loads(path.read_text(encoding='utf-8')) == portable
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(path), ''))
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.No)
    panel._import_file()
    assert received[-1] == ('flow_import', {'payload': portable, 'missing_models': 'error'})
    panel.deleteLater()


def test_slow_export_cannot_redirect_response(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.set_flow({'id': 'draft', 'revision': 1, 'nodes': [], 'edges': []}, [])
    paths = iter([str(tmp_path / 'a.json'), str(tmp_path / 'b.json')])
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (next(paths), ''))
    sent = []
    panel.command.connect(lambda action, payload: sent.append((action, payload)))
    panel._export_file()
    panel.set_flow({'id': 'draft', 'revision': 1, 'nodes': [], 'edges': []}, [])
    assert not panel.export_button.isEnabled()
    panel._export_file()
    assert len(sent) == 1
    panel.accept_result('flow_export', {'title': 'first'})
    assert json.loads((tmp_path / 'a.json').read_text()) == {'title': 'first'}
    assert not (tmp_path / 'b.json').exists()
    assert panel.export_button.isEnabled()
    panel._export_file()
    panel.accept_result('flow_export', {'title': 'second'})
    assert json.loads((tmp_path / 'b.json').read_text()) == {'title': 'second'}
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (str(tmp_path / 'c.json'), ''))
    panel._export_file()
    panel.accept_result('flow_export', {'error': True})
    assert panel.export_button.isEnabled()
    assert not (tmp_path / 'c.json').exists()
    panel.deleteLater()


@pytest.mark.parametrize('payload, prompts', [
    ({'model_profile': None, 'nodes': []}, 0),
    ({'model_profile': 'known', 'nodes': [{'model_profile': 'known'}]}, 0),
    ({'model_profile': 'missing', 'nodes': []}, 1),
    ({'nodes': [{'model_profile': 'missing'}]}, 1),
    ({'nodes': 'invalid'}, 0),
    ({'model_profile': {}, 'nodes': [None, 1, {'model_profile': []}]}, 0),
    ([], 0),
])
def test_import_only_asks_for_missing_model(tmp_path, monkeypatch, payload, prompts):
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.set_flow(None, [{'id': 'known', 'name': 'Known'}])
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(path), ''))
    questions = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: questions.append(args) or QMessageBox.StandardButton.Yes)
    sent = []
    panel.command.connect(lambda action, args: sent.append((action, args)))
    panel._import_file()
    assert len(questions) == prompts
    assert sent == [('flow_import', {'payload': payload, 'missing_models': 'inherit' if prompts else 'error'})]
    if questions:
        assert questions[0][-1] == QMessageBox.StandardButton.No
    panel.deleteLater()


def test_design_second_turn_receives_template_and_audits(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    asyncio.run(_design_second_turn_receives_template_and_audits(tmp_path, monkeypatch))


async def _design_second_turn_receives_template_and_audits(tmp_path, monkeypatch):
    import agent_core.core.flow_designer as core_designer
    calls, audits = [], []
    template = {'format': 'agent-flow', 'version': 1, 'nodes': [{'id': 'third', 'title': '第三节点'}]}

    async def design(_manager, messages, **_kwargs):
        calls.append(messages)
        return {'reply': '已设计', 'template': template}

    monkeypatch.setattr(core_designer, 'design_flow', design)
    bridge = RuntimeBridge(tmp_path)
    bridge._mutations = asyncio.Lock()
    bridge._flows = SimpleNamespace(import_template=lambda payload, **kw: {'id': 'new-flow'})
    monkeypatch.setattr(bridge, '_audit', lambda event, **kw: audits.append((event, kw)))
    panel = FlowPanel()
    panel.designer.command.connect(lambda action, args: bridge._schedule_command(action, args))
    bridge.finished.connect(lambda action, payload: panel.accept_result(action, json.loads(payload)))
    panel.designer.input.setText('设计流程')
    panel.designer.submit()
    await asyncio.gather(*list(bridge._commands))
    panel.designer.input.setText('修改第三节点')
    panel.designer.submit()
    await asyncio.gather(*list(bridge._commands))
    assert json.loads(calls[1][-2]['content'])['template'] == template
    assert len(audits) == 2
    assert '"format"' not in panel.designer.transcript.toPlainText()
    panel.deleteLater()


def test_oversize_design_template_is_explicitly_excluded():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    designer = panel.designer
    designer.input.setText('设计')
    designer.submit()
    designer.accept_result({'request_id': designer.request_id, 'reply': '完成', 'template': {'prompt': 'a' * 16000}})
    assert '模板过长' in designer.transcript.toPlainText()
    assert all(len(row['content']) <= 16000 for row in designer.history)
    panel.deleteLater()


def test_import_export_audited_without_file_path(tmp_path, monkeypatch):
    async def check():
        audits = []
        bridge = RuntimeBridge(tmp_path)
        bridge._manager = SimpleNamespace(sessions={})
        bridge._flows = SimpleNamespace(
            export_template=lambda definition: {'format': 'agent-flow'},
            import_template=lambda payload, **kw: {'id': 'copy'},
        )
        monkeypatch.setattr(bridge, '_audit', lambda event, **kw: audits.append((event, kw)))
        await bridge._dispatch('flow_export', {'definition': {'id': 'draft'}})
        await bridge._dispatch('flow_import', {'payload': {'format': 'agent-flow'}})
        assert audits == [('flow_export', {'flow_id': 'draft'}), ('flow_import', {'flow_id': 'copy'})]
    asyncio.run(check())


def test_window_can_resize_to_450_pixels(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(RuntimeBridge(tmp_path))
    assert window.minimumWidth() <= 450
    window.resize(450, 900)
    assert window.width() == 450
    window.close()


def test_narrow_designer_keeps_canvas_visible():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.resize(900, 700)
    panel.show()
    app.processEvents()
    panel.resize(450, 700)
    panel.designer_button.setChecked(True)
    app.processEvents()
    assert not panel.inspector.isVisible()
    assert panel.canvas.isVisible()
    assert panel.designer.width() <= 230
    assert panel.canvas.width() >= 170
    assert panel.designer.choices.direction() == QBoxLayout.Direction.TopToBottom
    panel._toggle_inspector()
    app.processEvents()
    assert panel.inspector.isVisible()
    assert not panel.designer.isVisible()
    assert panel.designer_button.isChecked()
    panel._toggle_inspector()
    assert panel.designer.isVisible()
    panel.close()


def test_deferred_project_update_preserves_submitted_flow_input():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    requests = []
    panel.command.connect(lambda action, args: requests.append((action, args)))
    project = {'id': 'project', 'revision': 1, 'handoff': ''}
    panel.set_flow({'id': 'flow', 'project_id': project['id'], 'project': project,
                    'revision': 1, 'nodes': [], 'edges': []}, [])
    panel.flow_fields['handoff'].setPlainText('新项目背景')
    panel.run_input.setPlainText('本次已提交\n第一批反馈')
    panel._submit('flow_run')
    assert [action for action, _ in requests] == ['project_update']

    panel.run_input.setPlainText('下次待提交\n第二批反馈')
    panel.accept_result('project_update', {**project, **requests[0][1]['changes'], 'revision': 2})
    action, payload = requests[1]
    assert action == 'flow_run'
    assert payload['input_text'] == '本次已提交\n第一批反馈'
    panel.accept_result('flow_run', {**payload['definition'], 'revision': 2})
    assert panel.run_input.toPlainText() == '下次待提交\n第二批反馈'
    panel.deleteLater()
