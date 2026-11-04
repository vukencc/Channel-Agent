"""原生窗口与真实 Core Flow 的集成；模型回复为明确受控回调。"""
import asyncio
import copy
import json
import os
import sys
import threading
import time

import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from agent_core import config
from agent_frontend.desktop.runtime import RuntimeBridge
from agent_frontend.desktop.window import MainWindow


def wait(app, predicate, seconds=8, diagnostic=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        # Qt 事件处理后主动让出 Python 线程，避免后台运行时被测试等待循环饿死。
        time.sleep(0.01)
    assert predicate(), f'等待 Flow 窗口超时：{diagnostic() if diagnostic else "无运行状态"}'


def wait_for_completed_flow(app, window):
    def run_state():
        return (window.state.get('selected_flow') or {}).get('run') or {}

    def diagnostic():
        run = run_state()
        nodes = {key: {'status': node.get('status'), 'error': node.get('error')}
                 for key, node in run.get('nodes', {}).items()}
        return {'status': run.get('status'), 'error': run.get('error'),
                'current_node': run.get('current_node'), 'nodes': nodes}

    wait(app, lambda: run_state().get('status') in {'completed', 'failed', 'cancelled'},
         diagnostic=diagnostic)
    assert run_state().get('status') == 'completed', f'Flow 未完成：{diagnostic()}'


def test_flow_wait_reports_terminal_failure_immediately(monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = type('Window', (), {'state': {'selected_flow': {'run': {
        'status': 'failed', 'error': '受控错误', 'current_node': 'first',
        'nodes': {'first': {'status': 'failed', 'error': '节点错误'}},
    }}}})()
    monkeypatch.setattr(time, 'sleep', lambda _: pytest.fail('终态不应继续等待'))
    with pytest.raises(AssertionError, match='受控错误.*节点错误'):
        wait_for_completed_flow(app, window)


def test_wait_allows_background_python_thread_to_progress():
    app = QApplication.instance() or QApplication([])
    affinity = os.sched_getaffinity(0) if hasattr(os, 'sched_getaffinity') else None
    previous_interval = sys.getswitchinterval()
    started, proceed, done = threading.Event(), threading.Event(), threading.Event()

    def worker():
        started.set()
        proceed.wait()
        done.set()

    thread = None
    try:
        if affinity is not None:
            os.sched_setaffinity(0, {min(affinity)})
        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(1)
        sys.setswitchinterval(10)
        proceed.set()
        wait(app, done.is_set, seconds=0.5)
    finally:
        sys.setswitchinterval(previous_interval)
        proceed.set()
        if thread is not None:
            thread.join(timeout=1)
        if affinity is not None:
            os.sched_setaffinity(0, affinity)


@pytest.fixture
def flow_desktop(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'HANDOFF.md').write_text('初始交接：验证真实项目传递', encoding='utf-8')
    prompts = []

    async def model(history, **kwargs):
        prompts.append(copy.deepcopy(history))
        if history[0]['content'].startswith('等待取消'):
            await asyncio.sleep(60)
        return {'role': 'assistant', 'content': '已完成节点，交接标记：next-step'}

    bridge = RuntimeBridge(tmp_path / 'state', workspace_root=tmp_path / 'workspace', model=model)
    window = MainWindow(bridge)
    window.show()
    bridge.start()
    wait(app, lambda: window.current_session is not None)
    yield app, window, bridge, project, prompts
    window.close()
    wait(app, lambda: not bridge.is_running and not bridge.worker_alive, 15)
    window.deleteLater()
    bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def create_flow(app, window, bridge, project):
    bridge.request('flow_create', title='有向交接模板')
    wait(app, lambda: bool(window.state.get('selected_flow')))
    flow = copy.deepcopy(window.state['selected_flow'])
    flow['nodes'] = [
        {'id': 'first', 'title': '准备', 'prompt': '先检查项目', 'x': 0, 'y': 0,
         'handoff_file': 'HANDOFF.md'},
        {'id': 'next', 'title': '验证', 'prompt': '再验证项目', 'x': 280, 'y': 0},
    ]
    flow['edges'] = [{'source': 'first', 'target': 'next'}]
    bridge.request('flow_save', definition=flow)
    wait(app, lambda: window.state['selected_flow']['revision'] > flow['revision'])
    template_id = flow['id']
    bridge.request('project_create', title='有向交接项目', workspace_path=str(project), flow_id=template_id)
    wait(app, lambda: (window.state.get('selected_flow') or {}).get('project_id'))
    instance = copy.deepcopy(window.state['selected_flow'])
    instance.pop('project', None)
    return instance


def test_flow_creation_run_handoff_and_classic_mode_are_independent(flow_desktop):
    app, window, bridge, project, prompts = flow_desktop
    classic = window.current_session['id']
    flow = create_flow(app, window, bridge, project)
    assert window.workspace_stack.currentWidget() is window.flow_page
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait_for_completed_flow(app, window)
    run = window.state['selected_flow']['run']
    assert '初始交接' in run['nodes']['first']['received_handoff']
    assert 'next-step' in run['nodes']['next']['received_handoff']
    assert len(prompts) == 2
    assert len(window.state['flows']) == 2
    bridge.request('select', session_id=classic)
    wait(app, lambda: window.state.get('selected_flow') is None)
    assert window.workspace_stack.currentWidget() is window.agent_page
    assert window.current_session['id'] == classic
    bridge.request('project_select', project_id=flow['project_id'])
    wait(app, lambda: bool(window.state.get('selected_flow')))
    assert window.state['selected_flow']['run']['status'] == 'completed'


def test_flow_cancel_blocks_successors_and_keeps_graph(flow_desktop):
    app, window, bridge, project, _ = flow_desktop
    flow = create_flow(app, window, bridge, project)
    flow['nodes'][0]['prompt'] = '等待取消'
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait(app, lambda: (window.state['selected_flow'].get('run') or {}).get('status') == 'running')
    QTest.mouseClick(window.flow_panel.run_button, Qt.MouseButton.LeftButton)
    wait(app, lambda: window.state['selected_flow']['run']['status'] == 'cancelled')
    assert window.state['selected_flow']['run']['nodes']['next']['status'] == 'blocked'
    assert len(window.state['selected_flow']['nodes']) == 2


def test_new_project_dialog_selects_agent_or_flow(flow_desktop):
    from agent_frontend.desktop.project_dialog import ProjectDialog
    _, window, _, project, _ = flow_desktop
    dialog = ProjectDialog(window, templates=[{'id': 'template-1', 'title': '模板'}])
    dialog.title.setText('项目名')
    dialog.mode.setCurrentIndex(dialog.mode.findData('flow'))
    dialog.workspace.setText(str(project))
    dialog.handoff.setPlainText('用户输入交接')
    dialog.accept()
    assert dialog.result_payload['mode'] == 'flow'
    assert dialog.result_payload['flow_id'] == 'template-1'
    assert dialog.result_payload['handoff'] == '用户输入交接'
    dialog.deleteLater()


def test_template_panel_cannot_run_and_project_settings_are_saved_first(flow_desktop):
    from agent_frontend.desktop.flow import FlowPanel
    panel = FlowPanel()
    commands = []
    panel.command.connect(lambda action, payload: commands.append((action, payload)))
    template = {'id': 'template', 'title': '模板', 'nodes': [], 'edges': [], 'revision': 1}
    panel.set_flow(template)
    panel._submit('flow_run')
    assert not commands
    assert panel.run_button.isHidden()
    project = {'id': 'project', 'revision': 2, 'workspace_path': '/tmp/project',
               'permission_policy': 'smart', 'execution_environment': 'sandbox', 'model_profile': None, 'handoff': ''}
    panel.set_flow({**template, 'id': 'instance', 'project_id': 'project', 'project': project})
    panel._field_changed('permission_policy', 'readonly', False)
    panel._submit('flow_save')
    assert commands[-1][0] == 'project_update'
    assert commands[-1][1]['changes']['permission_policy'] == 'readonly'
    panel.accept_result('project_update', {**project, 'revision': 3, 'permission_policy': 'readonly'})
    assert commands[-1][0] == 'flow_save'
    assert 'project' not in commands[-1][1]['definition']
    panel.deleteLater()


def test_project_save_response_after_switch_stays_with_original_flow(flow_desktop):
    from agent_frontend.desktop.flow import FlowPanel
    panel = FlowPanel()
    commands = []
    panel.command.connect(lambda action, payload: commands.append((action, payload)))
    a = {'id': 'a', 'title': 'A', 'nodes': [], 'edges': [], 'revision': 1, 'project_id': 'pa',
         'project': {'id': 'pa', 'revision': 1, 'handoff': '', 'permission_policy': 'smart'}}
    b = {'id': 'b', 'title': 'B', 'nodes': [], 'edges': [], 'revision': 1, 'project_id': 'pb',
         'project': {'id': 'pb', 'revision': 7, 'handoff': '', 'permission_policy': 'smart'}}
    panel.set_flow(a)
    panel._field_changed('permission_policy', 'readonly', False)
    panel._submit('flow_run')
    panel.set_flow(b)
    panel.accept_result('project_update', {'id': 'pa', 'revision': 2, 'permission_policy': 'readonly'})
    assert panel.draft()['project']['id'] == 'pb'
    assert panel.draft()['project']['revision'] == 7
    assert commands[-1][0] == 'flow_run'
    assert commands[-1][1]['flow_id'] == 'a'
    panel.deleteLater()


def test_close_saves_project_before_definition_and_stops_on_failure(flow_desktop, monkeypatch):
    _, window, bridge, _, _ = flow_desktop
    calls = []
    monkeypatch.setattr(bridge, 'request', lambda action, **args: calls.append((action, args)))
    draft = {'id': 'a', 'title': 'A', 'revision': 1, 'nodes': [], 'edges': [], 'project_id': 'pa',
             'project': {'id': 'pa', 'revision': 3, 'handoff': '背景', 'permission_policy': 'readonly',
                         'execution_environment': 'sandbox', 'model_profile': None}}
    window._pending_close_drafts = {'a': draft}
    window.flow_panel._project_dirty_cache['a'] = True
    window._close_save_next()
    assert calls[-1][0] == 'project_update'
    window._finished('project_update', json.dumps({'id': 'pa', 'revision': 4}))
    assert calls[-1][0] == 'flow_save'
    assert 'project' not in calls[-1][1]['definition']
    window._finished('flow_save', json.dumps({'error': '保存失败'}))
    assert window._pending_close_drafts is None
    assert window.centralWidget().isEnabled()


def test_directory_update_keeps_unsaved_settings_after_switch(flow_desktop):
    from agent_frontend.desktop.flow import FlowPanel
    panel = FlowPanel()
    a = {'id': 'a', 'title': 'A', 'nodes': [], 'edges': [], 'revision': 1, 'project_id': 'pa',
         'project': {'id': 'pa', 'revision': 1, 'workspace_path': '/old', 'handoff': '', 'permission_policy': 'smart'}}
    b = {'id': 'b', 'title': 'B', 'nodes': [], 'edges': [], 'revision': 1, 'project_id': 'pb',
         'project': {'id': 'pb', 'revision': 3}}
    panel.set_flow(a)
    panel._field_changed('permission_policy', 'readonly', False)
    panel._directory_update_id = ('a', 'pa')
    panel.set_flow(b)
    panel.accept_result('project_update', {'id': 'pa', 'revision': 2, 'workspace_path': '/new', 'permission_policy': 'smart'})
    assert panel.draft()['project']['id'] == 'pb'
    panel.set_flow(a)
    assert panel.draft()['project']['workspace_path'] == '/new'
    assert panel.draft()['project']['revision'] == 2
    assert panel.draft()['project']['permission_policy'] == 'readonly'
    panel.deleteLater()


def test_deleting_dirty_project_discards_only_its_cached_draft(flow_desktop):
    from agent_frontend.desktop.flow import FlowPanel
    panel = FlowPanel()
    project = {'id': 'a', 'title': 'A', 'nodes': [], 'edges': [], 'revision': 1, 'project_id': 'pa',
               'project': {'id': 'pa', 'revision': 1, 'permission_policy': 'smart'}}
    template = {'id': 'b', 'title': 'B', 'nodes': [], 'edges': [], 'revision': 1}
    panel.set_flow(project)
    panel._field_changed('permission_policy', 'readonly', False)
    panel.set_flow(template)
    panel.add_node()
    panel.accept_result('project_delete', {'project_id': 'pa', 'flow_id': 'a'})
    assert [draft['id'] for draft in panel.pending_drafts()] == ['b']
    assert 'a' not in panel._project_dirty_cache
    panel.deleteLater()


@pytest.mark.parametrize('switch', [False, True])
def test_project_edit_after_save_started_remains_dirty(flow_desktop, switch):
    from agent_frontend.desktop.flow import FlowPanel
    panel = FlowPanel()
    commands = []
    panel.command.connect(lambda action, payload: commands.append((action, payload)))
    flow = {'id': 'a', 'title': 'A', 'nodes': [], 'edges': [], 'revision': 1, 'project_id': 'pa',
            'project': {'id': 'pa', 'revision': 1, 'handoff': '', 'permission_policy': 'smart'}}
    panel.set_flow(flow)
    panel._field_changed('handoff', 'first', False)
    panel._submit('flow_save')
    panel._field_changed('handoff', 'newer', False)
    if switch:
        panel.set_flow({'id': 'b', 'title': 'B', 'nodes': [], 'edges': [], 'revision': 1})
    panel.accept_result('project_update', {'id': 'pa', 'revision': 2, 'handoff': 'first', 'permission_policy': 'smart'})
    panel.accept_result('flow_save', {**flow, 'revision': 2})
    if switch:
        panel.set_flow(flow)
    assert panel.draft()['project']['handoff'] == 'newer'
    assert panel.is_dirty
    assert panel.heading.text().endswith(' *')
    assert [draft['id'] for draft in panel.pending_drafts()] == ['a']
    panel.deleteLater()


def test_projects_and_templates_have_separate_lists_and_project_title(flow_desktop, monkeypatch):
    _, window, bridge, _, _ = flow_desktop
    calls = []
    monkeypatch.setattr(bridge, 'request', lambda action, **args: calls.append((action, args)))
    window.state = {**window.state, 'projects': [{'id': 'p', 'title': '项目 A', 'flow_id': 'copy'}],
                    'flows': [{'id': 't', 'title': '模板', 'status': 'idle', 'node_count': 1, 'project_id': None},
                              {'id': 'copy', 'title': '模板', 'status': 'idle', 'node_count': 1, 'project_id': 'p'}],
                    'selected_flow': {'id': 'copy'}}
    window._render_flows()
    assert window.projects.count() == 1
    assert window.flows.count() == 1
    assert '项目 A' in window.projects.item(0).text()
    assert '模板' in window.flows.item(0).text()
    window._select_flow_item(window.projects.item(0))
    window._select_flow_item(window.flows.item(0))
    assert calls == [('project_select', {'project_id': 'p'}), ('flow_select', {'flow_id': 't'})]
    window.flow_panel.set_flow({'id': 'copy', 'title': '模板', 'project_id': 'p', 'project': {'id': 'p', 'title': '项目 A', 'workspace_path': '/project'}, 'nodes': [], 'edges': [], 'revision': 1})
    assert '项目 A' in window.flow_panel.heading.text()
    assert '项目工作区' in window.flow_panel.project_summary.text()
