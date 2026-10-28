"""受控 Qt 界面回归：不调用模型、服务或用户项目。"""
import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog, QLineEdit

from agent_frontend.desktop import project_picker
from agent_frontend.desktop.flow import FlowPanel
from agent_frontend.desktop.project_dialog import ProjectDialog
from agent_core.core.flows import FlowService, _order
from agent_core.core.workspaces import SessionWorkspaces


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    widget = FlowPanel()
    widget.resize(1000, 700)
    widget.show()
    yield widget
    widget.close()
    app.processEvents()


def flow(path='', *, identifier='flow', handoff=''):
    return {'id': identifier, 'title': '测试流程', 'revision': 1, 'run': None,
            'workspace_path': str(path), 'handoff': handoff,
            'nodes': [{'id': 'first', 'title': '梳理', 'prompt': '检查项目',
                       'workspace_path': '', 'handoff': '', 'x': 0, 'y': 0},
                      {'id': 'second', 'title': '实现', 'prompt': '实现任务',
                       'workspace_path': '', 'handoff': '', 'x': 240, 'y': 0}],
            'edges': [{'source': 'first', 'target': 'second'}]}


def test_flow_project_is_accessible_above_narrow_canvas(panel, tmp_path):
    panel.resize(560, 700)
    panel.set_flow(flow(tmp_path))
    QApplication.processEvents()
    assert panel.project_button.isVisible()
    assert str(tmp_path) in panel.project_summary.text()
    assert panel.project_button.geometry().bottom() < panel.canvas.geometry().top()


def choose(monkeypatch, result):
    """目录对话框受控替身；仍通过真实按钮与槽更新草稿。"""
    calls = []

    def selected(*args, **kwargs):
        calls.append((args, kwargs))
        return result() if callable(result) else str(result)

    monkeypatch.setattr(project_picker, 'choose_project_directory', selected)
    return calls


def click(button):
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()


@pytest.mark.parametrize('target', ['header', 'flow', 'node'])
def test_project_selection_updates_only_target_and_saved_draft(panel, tmp_path, monkeypatch, target):
    original, selected = tmp_path / 'original', tmp_path / 'selected'
    original.mkdir()
    selected.mkdir()
    panel.set_flow(flow(original))
    panel.nodes['first'].setSelected(True)
    assert isinstance(panel.flow_fields['workspace_path'], QLineEdit)
    assert isinstance(panel.node_fields['workspace_path'], QLineEdit)
    calls = choose(monkeypatch, selected)
    button = (panel.project_button if target == 'header' else
              panel.flow_project_picker.browse_button if target == 'flow' else
              panel.node_project_picker.browse_button)
    click(button)
    draft = panel.draft()
    assert len(calls) == 1
    assert panel.is_dirty
    assert draft['workspace_path'] == str(original if target == 'node' else selected)
    assert draft['nodes'][0]['workspace_path'] == (str(selected) if target == 'node' else '')
    assert draft['nodes'][1]['workspace_path'] == ''
    sent = []
    panel.command.connect(lambda action, payload: sent.append((action, payload)))
    click(panel.save_button)
    assert sent[-1] == ('flow_save', {'definition': draft})
    panel.set_flow(flow(identifier='other'))
    panel.set_flow(flow(original))
    assert panel.draft() == draft


@pytest.mark.parametrize('target', ['header', 'flow', 'node'])
def test_cancel_directory_selection_preserves_clean_draft(panel, tmp_path, monkeypatch, target):
    panel.set_flow(flow(tmp_path))
    panel.nodes['first'].setSelected(True)
    original = panel.draft()
    calls = choose(monkeypatch, '')
    button = (panel.project_button if target == 'header' else
              panel.flow_project_picker.browse_button if target == 'flow' else
              panel.node_project_picker.browse_button)
    click(button)
    assert len(calls) == 1
    assert panel.draft() == original
    assert not panel.is_dirty


@pytest.mark.parametrize('status', ['queued', 'running', 'cancelling'])
def test_all_project_buttons_are_locked_during_execution(panel, tmp_path, monkeypatch, status):
    definition = flow(tmp_path)
    definition['run'] = {'status': status, 'nodes': {}}
    panel.set_flow(definition)
    panel.nodes['first'].setSelected(True)
    calls = choose(monkeypatch, tmp_path / 'never-selected')
    for button in (panel.project_button, panel.flow_project_picker.browse_button,
                   panel.node_project_picker.browse_button):
        assert not button.isEnabled()
        click(button)
    assert not calls
    assert not panel.is_dirty
    assert panel.draft() == definition


@pytest.mark.parametrize(('target', 'transition'), [
    (target, transition) for target in ('header', 'flow', 'node')
    for transition in ('running', 'switch_flow')
] + [('node', 'switch_node')])
def test_modal_selection_rechecks_target_before_writing(panel, tmp_path, monkeypatch, target, transition):
    definition = flow(tmp_path)
    panel.set_flow(definition)
    panel.nodes['first'].setSelected(True)

    def change_while_choosing():
        if transition == 'running':
            panel.set_flow({**definition, 'run': {'status': 'running', 'nodes': {}}})
        elif transition == 'switch_flow':
            panel.set_flow(flow(tmp_path, identifier='other'))
        else:
            panel.nodes['first'].setSelected(False)
            panel.nodes['second'].setSelected(True)
        return str(tmp_path / 'stale-selection')

    calls = choose(monkeypatch, change_while_choosing)
    button = (panel.project_button if target == 'header' else
              panel.flow_project_picker.browse_button if target == 'flow' else
              panel.node_project_picker.browse_button)
    click(button)
    assert len(calls) == 1
    assert panel.draft()['workspace_path'] == str(tmp_path)
    assert all(node['workspace_path'] == '' for node in panel.draft()['nodes'])
    assert not panel.is_dirty
    if transition == 'switch_flow':
        panel.set_flow(definition)
        assert panel.draft() == definition


def test_optional_context_preserves_existing_values_and_prompt(panel, tmp_path):
    definition = flow(tmp_path, handoff='既有项目背景')
    definition['nodes'][0]['handoff'] = '节点补充背景'
    panel.set_flow(definition)
    panel.nodes['first'].setSelected(True)
    assert panel.flow_context_toggle.isChecked()
    assert panel.node_context_toggle.isChecked()
    assert not panel.flow_fields['handoff'].isHidden()
    assert not panel.node_fields['handoff'].isHidden()
    click(panel.node_context_toggle)
    assert panel.draft()['nodes'][0]['handoff'] == '节点补充背景'
    assert panel.draft()['nodes'][0]['prompt'] == '检查项目'
    panel.nodes['first'].setSelected(False)
    panel.nodes['second'].setSelected(True)
    assert not panel.node_context_toggle.isChecked()
    assert panel.draft()['nodes'][1]['handoff'] == ''
    assert not panel.is_dirty


def test_empty_context_does_not_block_run_or_add_manual_handoff(panel, tmp_path):
    panel.set_flow(flow(tmp_path))
    panel.nodes['first'].setSelected(True)
    assert not panel.flow_context_toggle.isChecked()
    assert not panel.node_context_toggle.isChecked()
    sent = []
    panel.command.connect(lambda action, payload: sent.append((action, payload)))
    click(panel.run_button)
    assert sent[-1][0] == 'flow_run'
    definition = sent[-1][1]['definition']
    assert definition['handoff'] == ''
    assert definition['nodes'][0]['handoff'] == ''
    assert definition['edges'] == [{'source': 'first', 'target': 'second'}]


def test_node_summary_explains_inherited_and_independent_projects(panel, tmp_path):
    inherited, independent = tmp_path / 'shared', tmp_path / 'independent'
    inherited.mkdir()
    independent.mkdir()
    definition = flow(inherited)
    definition['nodes'].append({'id': 'root', 'title': '独立根', 'prompt': '独立任务',
                                'workspace_path': str(independent), 'x': 0, 'y': 200})
    panel.set_flow(definition)
    panel.nodes['second'].setSelected(True)
    assert str(inherited) in panel.node_project_summary.text()
    assert '上游' in panel.node_project_summary.text()
    panel.nodes['second'].setSelected(False)
    panel.nodes['root'].setSelected(True)
    assert str(independent) in panel.node_project_summary.text()
    assert panel.draft()['nodes'][1]['workspace_path'] == ''


def test_multiple_upstream_projects_show_conflict(panel, tmp_path):
    definition = flow(tmp_path / 'one')
    definition['nodes'].append({'id': 'root', 'title': '另一根', 'prompt': '任务',
                                'workspace_path': str(tmp_path / 'two'), 'x': 0, 'y': 200})
    definition['edges'].append({'source': 'root', 'target': 'second'})
    panel.set_flow(definition)
    panel.nodes['second'].setSelected(True)
    assert '冲突' in panel.node_project_summary.text()
    assert panel.draft()['nodes'][1]['workspace_path'] == ''


def test_explicit_downstream_project_mismatch_is_not_reported_as_effective(panel, tmp_path):
    definition = flow(tmp_path / 'upstream')
    definition['nodes'][1]['workspace_path'] = str(tmp_path / 'different')
    panel.set_flow(definition)
    panel.nodes['second'].setSelected(True)
    assert '冲突' in panel.node_project_summary.text()
    assert panel.draft()['nodes'][1]['workspace_path'] == str(tmp_path / 'different')


def test_failed_save_keeps_selected_project_and_context(panel, tmp_path, monkeypatch):
    panel.set_flow(flow(tmp_path, handoff='必须保留'))
    selected = tmp_path / 'selected'
    selected.mkdir()
    choose(monkeypatch, selected)
    click(panel.project_button)
    click(panel.save_button)
    panel.accept_result('flow_save', {'ok': False, 'error': '项目校验失败'})
    assert panel.is_dirty
    assert panel.draft()['workspace_path'] == str(selected)
    assert panel.draft()['handoff'] == '必须保留'
    assert panel.flow_fields['workspace_path'].text() == str(selected)
    assert panel.pending_drafts()[0]['workspace_path'] == str(selected)


@pytest.mark.parametrize('mode', ['agent', 'flow'])
def test_new_project_picker_preserves_payload_and_permission(mode, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    dialog = ProjectDialog(mode=mode)
    dialog.show()
    try:
        assert isinstance(dialog.workspace, QLineEdit)
        assert not dialog.context_toggle.isChecked()
        calls = choose(monkeypatch, tmp_path)
        click(dialog.project_picker.browse_button)
        assert len(calls) == 1
        assert dialog.workspace.text() == str(tmp_path)
        dialog.policy.setCurrentIndex(dialog.policy.findData('readonly'))
        dialog.title.setText('新项目')
        if mode == 'flow':
            click(dialog.context_toggle)
            dialog.handoff.setPlainText('现有背景')
        dialog.accept()
        assert dialog.result_payload == {
            'mode': mode, 'title': '新项目', 'workspace_path': str(tmp_path),
            'execution_environment': None, 'permission_policy': 'readonly',
            'handoff': '现有背景' if mode == 'flow' else '',
        }
    finally:
        dialog.close()
        app.processEvents()


def core_projects(definition, tmp_path):
    """仅调用真实 Core 项目校验，不创建服务、会话或模型。"""
    manager = SimpleNamespace(sessions={}, store=SimpleNamespace(
        root=tmp_path / 'state', workspace_root=tmp_path / 'workspaces'))
    manager.workspaces = SessionWorkspaces(manager)
    order, incoming = _order(definition)
    return FlowService._projects(SimpleNamespace(manager=manager), definition, order, incoming)


@pytest.mark.parametrize('suffix', ['/', '/.'])
@pytest.mark.parametrize('topology', ['downstream_override', 'shared_upstreams'])
def test_equivalent_project_paths_do_not_report_inheritance_conflict(panel, tmp_path, suffix, topology):
    project = tmp_path / 'project'
    project.mkdir()
    definition = flow(project)
    alternate = str(project) + suffix
    if topology == 'downstream_override':
        definition['nodes'][1]['workspace_path'] = alternate
    else:
        definition['nodes'].append({'id': 'root', 'title': '等价项目起点', 'prompt': '检查',
                                    'workspace_path': alternate, 'x': 0, 'y': 200})
        definition['edges'].append({'source': 'root', 'target': 'second'})
    projects = core_projects(definition, tmp_path)
    assert len(set(projects.values())) == 1
    panel.set_flow(definition)
    panel.nodes['second'].setSelected(True)
    summary = panel.node_project_summary.text()
    assert '冲突' not in summary
    assert '生效项目' in summary
    assert str(project) in summary
    assert panel.draft()['nodes'][1]['workspace_path'] == (
        alternate if topology == 'downstream_override' else '')


@pytest.mark.parametrize('invalid_kind', ['parent', 'relative', 'literal_home', 'double_root'])
def test_invalid_project_is_rejected_by_core_and_not_reported_effective(panel, tmp_path, invalid_kind):
    project = tmp_path / 'project'
    (project / 'child').mkdir(parents=True)
    definition = flow(project)
    invalid = {'parent': str(project) + '/child/..', 'relative': 'project',
               'literal_home': '~/project', 'double_root': '/' + str(project)}[invalid_kind]
    definition['nodes'][1]['workspace_path'] = invalid
    expected_error = PermissionError if invalid_kind == 'double_root' else ValueError
    with pytest.raises(expected_error):
        core_projects(definition, tmp_path)
    panel.set_flow(definition)
    panel.nodes['second'].setSelected(True)
    summary = panel.node_project_summary.text()
    assert '生效项目' not in summary
    assert any(word in summary for word in ('校验', '无效', '不合法'))
    assert panel.draft()['nodes'][1]['workspace_path'] == invalid


def test_shared_ancestors_are_resolved_in_linear_visits(panel, tmp_path):
    """计数实际节点读取，避免机器速度影响复杂度回归。"""
    definition = flow(tmp_path)
    definition['nodes'] = [{'id': 'root', 'title': '起点', 'prompt': '检查', 'x': 0, 'y': 0}]
    definition['edges'] = []
    previous = ['root']
    for level in range(12):
        current = [f'{level}-a', f'{level}-b']
        for identifier in current:
            definition['nodes'].append({'id': identifier, 'title': identifier,
                                        'prompt': '检查', 'x': level * 220, 'y': 100})
            definition['edges'].extend({'source': source, 'target': identifier} for source in previous)
        previous = current
    panel.set_flow(definition)
    accesses = []

    class CountedNode(dict):
        def get(self, key, default=None):
            accesses.append(key)
            return super().get(key, default)

    for item in panel.nodes.values():
        item.data = CountedNode(item.data)
    panel._selected = previous[-1]
    panel._refresh_projects()
    assert '生效项目' in panel.node_project_summary.text()
    assert len(accesses) <= 12 * (len(definition['nodes']) + len(definition['edges']))


def test_inheritance_cycle_still_has_explicit_invalid_summary(panel, tmp_path):
    definition = flow(tmp_path)
    definition['edges'].append({'source': 'second', 'target': 'first'})
    panel.set_flow(definition)
    panel.nodes['second'].setSelected(True)
    summary = panel.node_project_summary.text()
    assert '循环' in summary
    assert '生效项目' not in summary


def test_inherited_summary_refreshes_after_upstream_project_edit(panel, tmp_path):
    first, changed = tmp_path / 'first', tmp_path / 'changed'
    first.mkdir()
    changed.mkdir()
    panel.set_flow(flow(first))
    panel.nodes['second'].setSelected(True)
    assert str(first) in panel.node_project_summary.text()
    panel.nodes['second'].setSelected(False)
    panel.nodes['first'].setSelected(True)
    panel.node_fields['workspace_path'].setText(str(changed))
    panel.nodes['first'].setSelected(False)
    panel.nodes['second'].setSelected(True)
    assert str(changed) in panel.node_project_summary.text()
    assert str(first) not in panel.node_project_summary.text()
    assert panel.draft()['nodes'][1]['workspace_path'] == ''


def test_independent_roots_need_no_flow_default_project(panel, tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    definition = flow()
    definition['edges'] = []
    definition['nodes'][0]['workspace_path'] = str(first)
    definition['nodes'][1]['workspace_path'] = str(second)
    panel.set_flow(definition)
    assert '流程默认项目' in panel.project_summary.text()
    assert '起点' in panel.project_summary.text()
    assert '已有' in panel.project_summary.text()
    assert '校验' in panel.project_summary.text()
    for identifier, path in [('first', first), ('second', second)]:
        panel.canvas.scene().clearSelection()
        panel.nodes[identifier].setSelected(True)
        assert str(path) in panel.node_project_summary.text()
        assert '冲突' not in panel.node_project_summary.text()
    sent = []
    panel.command.connect(lambda action, payload: sent.append((action, payload)))
    click(panel.run_button)
    assert sent[-1][0] == 'flow_run'
    assert sent[-1][1]['definition']['workspace_path'] == ''
    assert [node['workspace_path'] for node in sent[-1][1]['definition']['nodes']] == [str(first), str(second)]


@pytest.mark.parametrize('accept', [False, True])
@pytest.mark.parametrize('empty_path', [False, True])
def test_real_project_dialog_options_and_accept_cancel(panel, tmp_path, monkeypatch, accept, empty_path):
    """真实非原生目录窗口，定时点击其接受或取消按钮。"""
    home = tmp_path / 'home'
    home.mkdir()
    selected = home / 'selected'
    selected.mkdir()
    initial = tmp_path / 'initial'
    initial.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'qt-config'))
    observed, timed_out = [], []

    def finish():
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, QFileDialog):
            observed.append({'wrong_modal': type(dialog).__name__})
            if dialog:
                dialog.reject()
            return
        boxes = dialog.findChildren(QDialogButtonBox)
        observed.append({
            'directory': dialog.directory().absolutePath(),
            'mode': dialog.fileMode(), 'options': dialog.options(),
            'accept': dialog.labelText(QFileDialog.DialogLabel.Accept),
            'reject': dialog.labelText(QFileDialog.DialogLabel.Reject),
            'icons_empty': all(button.icon().isNull() for box in boxes for button in box.buttons()),
        })
        if accept:
            dialog.setDirectory(str(selected))
        button = boxes[0].button(QDialogButtonBox.StandardButton.Open if accept
                                 else QDialogButtonBox.StandardButton.Cancel)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

    def deadline():
        timed_out.append(True)
        dialog = QApplication.activeModalWidget()
        if dialog:
            dialog.reject()

    timer, watchdog = QTimer(panel), QTimer(panel)
    timer.setSingleShot(True)
    watchdog.setSingleShot(True)
    timer.timeout.connect(finish)
    watchdog.timeout.connect(deadline)
    timer.start(0)
    watchdog.start(3000)
    try:
        result = project_picker.choose_project_directory(panel, '' if empty_path else str(initial))
    finally:
        timer.stop()
        watchdog.stop()
    assert not timed_out
    assert len(observed) == 1
    details = observed[0]
    assert details['directory'] == str(home if empty_path else initial)
    assert details['mode'] == QFileDialog.FileMode.Directory
    for option in (QFileDialog.Option.DontUseNativeDialog, QFileDialog.Option.ShowDirsOnly,
                   QFileDialog.Option.ReadOnly, QFileDialog.Option.DontResolveSymlinks):
        assert details['options'] & option
    assert details['accept'] == '选择项目'
    assert details['reject'] == '取消'
    assert details['icons_empty']
    assert result == (str(selected) if accept else '')
