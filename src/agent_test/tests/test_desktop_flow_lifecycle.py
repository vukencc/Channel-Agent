"""真实 Qt/Core 生命周期回归；离线模型回调与消息框选择均为受控 UI fixture。"""
import asyncio
import copy
import json
import time

import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from agent_core import config
from agent_core.api import SessionStore
from agent_frontend.desktop.runtime import RuntimeBridge
from agent_frontend.desktop.window import MainWindow


def wait(app, predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), '等待真实 Flow 生命周期状态超时'


@pytest.fixture
def lifecycle(tmp_path, monkeypatch, sandbox_env):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'standard')
    monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Discard)
    project = tmp_path / 'project'
    project.mkdir()
    calls = []
    clients = []

    async def model(history, **kwargs):
        calls.append(copy.deepcopy(history))
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': '工具结果已接收，结束节点'}
        instruction = history[0]['content']
        if instruction.startswith('等待取消'):
            await asyncio.sleep(60)
        if instruction.startswith('请求写入'):
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'controlled-flow-write', 'function': {'name': 'create_file',
                'arguments': json.dumps({'path': 'approval.txt', 'content': 'approved'})}}]}
        return {'role': 'assistant', 'content': '受控节点已完成'}

    def open_window():
        bridge = RuntimeBridge(tmp_path / 'state', workspace_root=sandbox_env, model=model)
        window = MainWindow(bridge)
        clients.append((window, bridge))
        window.show()
        bridge.start()
        wait(app, lambda: window.current_session is not None)
        return window, bridge

    yield app, open_window, project, calls
    for window, bridge in reversed(clients):
        # 失败时也先排空真实线程，避免未处理的模态选择阻塞清理。
        bridge.shutdown()
        wait(app, lambda: not bridge.is_running and not bridge.worker_alive, 15)
        window.close()
        window.deleteLater()
        bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def create_flow(app, window, bridge, project, prompt='受控初始提示'):
    bridge.request('flow_create', title='生命周期项目', workspace_path=str(project), handoff='初始交接')
    wait(app, lambda: bool(window.state.get('selected_flow')))
    definition = copy.deepcopy(window.state['selected_flow'])
    definition['nodes'] = [
        {'id': 'first', 'title': '首个节点', 'prompt': prompt, 'x': 0, 'y': 0},
        {'id': 'next', 'title': '后续节点', 'prompt': '后续检查', 'x': 280, 'y': 0},
    ]
    definition['edges'] = [{'source': 'first', 'target': 'next'}]
    return definition


def run_status(window):
    return ((window.state.get('selected_flow') or {}).get('run') or {}).get('status')


def test_flow_confirmation_rejects_wrong_binding_then_allows_current_request(lifecycle):
    app, open_window, project, _ = lifecycle
    window, bridge = open_window()
    classic = window.current_session['id']
    errors = []
    bridge.error.connect(errors.append)
    flow = create_flow(app, window, bridge, project, '请求写入')
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait(app, lambda: window.confirmation_dialog is not None)
    first_key = window._confirmation_key
    assert first_key[0] != classic
    assert window.workspace_stack.currentWidget() is window.flow_page
    assert window.state['selected_flow']['run']['nodes']['first']['session_id'] == first_key[0]
    window.confirmation_dialog.reject()
    wait(app, lambda: run_status(window) == 'completed')
    assert not (project / 'approval.txt').exists()

    bridge.request('flow_run', flow_id=flow['id'])
    wait(app, lambda: window.confirmation_dialog is not None)
    current_key = window._confirmation_key
    assert current_key != first_key
    for session_id, confirmation_id in [(classic, current_key[1]), (current_key[0], first_key[1])]:
        count = len(errors)
        bridge.request('decide', session_id=session_id, confirmation_id=confirmation_id, allowed=True)
        wait(app, lambda: len(errors) > count)
        assert window._confirmation_key == current_key
        assert window.confirmation_dialog is not None
        assert not (project / 'approval.txt').exists()
    window.confirmation_dialog.accept()
    wait(app, lambda: run_status(window) == 'completed')
    assert (project / 'approval.txt').read_text() == 'approved'


def test_flow_confirmation_timeout_does_not_write(lifecycle, monkeypatch):
    app, open_window, project, _ = lifecycle
    monkeypatch.setattr(config, 'CONFIRM_TIMEOUT', 0.4)
    window, bridge = open_window()
    flow = create_flow(app, window, bridge, project, '请求写入')
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait(app, lambda: window.confirmation_dialog is not None)
    wait(app, lambda: run_status(window) == 'completed')
    assert window.confirmation_dialog is None
    assert not (project / 'approval.txt').exists()


@pytest.mark.parametrize('policy, writes', [('readonly', False), ('full_access', True)])
def test_explicit_flow_permission_controls_real_write(lifecycle, policy, writes):
    app, open_window, project, calls = lifecycle
    window, bridge = open_window()
    flow = create_flow(app, window, bridge, project, '请求写入')
    flow['permission_policy'] = policy
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait(app, lambda: run_status(window) == 'completed')
    assert window.confirmation_dialog is None
    assert (project / 'approval.txt').exists() is writes
    if writes:
        assert (project / 'approval.txt').read_text() == 'approved'
    node_id = window.state['selected_flow']['run']['nodes']['first']['session_id']
    row = next(row for row in window.state['sessions'] if row['id'] == node_id)
    assert row['permission_policy'] == policy
    # 模型仅提出调用；真正的工具响应必须从 Core 回到下一轮模型历史。
    assert any(message.get('role') == 'tool' and message.get('tool_call_id') == 'controlled-flow-write'
               for history in calls for message in history)


def test_closing_running_flow_drains_and_reopens_without_model_replay(lifecycle):
    app, open_window, project, calls = lifecycle
    window, bridge = open_window()
    flow = create_flow(app, window, bridge, project, '等待取消')
    bridge.request('flow_run', flow_id=flow['id'], definition=flow)
    wait(app, lambda: run_status(window) == 'running' and len(calls) == 1)
    window.close()
    wait(app, lambda: not bridge.is_running and not bridge.worker_alive, 15)
    assert not bridge._manager.project_reservations
    assert not bridge._manager.project_reservation_nodes
    assert all(task.done() for task in bridge._flows.tasks.values())
    assert all(not item.busy and not item.tool_workers for item in bridge._manager.sessions.values())
    store = SessionStore(bridge.state_dir, workspace_root=bridge.workspace_root)
    store.close()

    reopened, new_bridge = open_window()
    new_bridge.request('flow_select', flow_id=flow['id'])
    wait(app, lambda: run_status(reopened) == 'cancelled')
    restored = reopened.state['selected_flow']
    assert [node['id'] for node in restored['nodes']] == ['first', 'next']
    assert restored['edges'] == flow['edges']
    assert restored['run']['nodes']['next']['status'] == 'blocked'
    QTest.qWait(200)
    app.processEvents()
    assert len(calls) == 1
    assert not new_bridge._manager.project_reservations


def edit_draft_and_switch(app, window, bridge, project, *, valid=True):
    classic = window.current_session['id']
    flow = create_flow(app, window, bridge, project)
    panel = window.flow_panel
    panel.add_node(QPointF(10, 20))
    first = panel._selected
    panel.prompt_edit.setPlainText('保存的初始提示' if valid else '')
    panel.add_node(QPointF(300, 20))
    second = panel._selected
    panel.prompt_edit.setPlainText('后续验证提示')
    panel.add_edge(first, second)
    panel.flow_fields['handoff'].setPlainText('保留的初始交接')
    bridge.request('select', session_id=classic)
    wait(app, lambda: window.state.get('selected_flow') is None)
    assert window.workspace_stack.currentWidget() is window.agent_page
    assert len(panel.pending_drafts()) == 1
    return flow['id'], first, second


def test_save_and_exit_persists_hidden_dirty_graph_and_prompts(lifecycle, monkeypatch):
    app, open_window, project, calls = lifecycle
    window, bridge = open_window()
    flow_id, first, second = edit_draft_and_switch(app, window, bridge, project)
    monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Save)
    window.close()
    wait(app, lambda: not bridge.is_running and not bridge.worker_alive, 15)
    stored = json.loads((bridge.state_dir / 'flows' / f'{flow_id}.json').read_text())
    assert stored['handoff'] == '保留的初始交接'
    assert stored['nodes'][0]['prompt'] == '保存的初始提示'
    assert stored['edges'] == [{'source': first, 'target': second}]
    reopened, new_bridge = open_window()
    new_bridge.request('flow_select', flow_id=flow_id)
    wait(app, lambda: bool(reopened.state.get('selected_flow')))
    assert reopened.flow_panel.draft()['nodes'] == stored['nodes']
    assert reopened.flow_panel.draft()['edges'] == stored['edges']
    assert not reopened.flow_panel.pending_drafts()
    assert calls == []


def test_save_validation_failure_keeps_hidden_draft_and_window_alive(lifecycle, monkeypatch):
    app, open_window, project, _ = lifecycle
    window, bridge = open_window()
    flow_id, first, _ = edit_draft_and_switch(app, window, bridge, project, valid=False)
    before = (bridge.state_dir / 'flows' / f'{flow_id}.json').read_bytes()
    errors = []
    bridge.error.connect(errors.append)
    monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Save)
    window.close()
    wait(app, lambda: bool(errors) and window._pending_close_drafts is None)
    assert bridge.is_running and bridge.worker_alive
    assert window.isVisible() and window.centralWidget().isEnabled()
    assert not window._closing
    assert (bridge.state_dir / 'flows' / f'{flow_id}.json').read_bytes() == before
    draft = window.flow_panel.pending_drafts()[0]
    assert draft['id'] == flow_id
    assert draft['nodes'][0]['id'] == first and draft['nodes'][0]['prompt'] == ''
    bridge.request('flow_select', flow_id=flow_id)
    wait(app, lambda: bool(window.state.get('selected_flow')))
    assert window.flow_panel.draft()['nodes'] == draft['nodes']


@pytest.mark.parametrize('binding', ['flow', 'node'])
def test_model_referenced_by_flow_cannot_be_deleted(lifecycle, binding):
    app, open_window, project, _ = lifecycle
    window, bridge = open_window()
    errors, replies = [], []
    bridge.error.connect(errors.append)
    bridge.finished.connect(lambda action, payload: replies.append((action, json.loads(payload))))
    bridge.request('model_add', name='离线 fixture 模型', model='controlled',
                   base_url='https://example.invalid/v1', api_key='fixture-only-placeholder')
    wait(app, lambda: bool(window.state['models']))
    profile = window.state['models'][0]['id']
    flow = create_flow(app, window, bridge, project)
    (flow if binding == 'flow' else flow['nodes'][0])['model_profile'] = profile
    bridge.request('flow_save', definition=flow)
    wait(app, lambda: any(action == 'flow_save' and not result.get('error') for action, result in replies))
    bridge.request('model_delete', model_id=profile)
    wait(app, lambda: bool(errors))
    assert 'Flow' in errors[-1]
    assert any(row['id'] == profile for row in window.state['models'])
    stored = json.loads((bridge.state_dir / 'flows' / f'{flow["id"]}.json').read_text())
    assert (stored if binding == 'flow' else stored['nodes'][0])['model_profile'] == profile


@pytest.mark.parametrize('keep_other_draft', [False, True])
def test_delete_dirty_flow_then_close_saves_only_remaining_drafts(lifecycle, monkeypatch, keep_other_draft):
    app, open_window, project, calls = lifecycle
    window, bridge = open_window()
    errors, replies = [], []
    bridge.error.connect(errors.append)
    bridge.finished.connect(lambda action, payload: replies.append((action, json.loads(payload))))
    retained_id = None
    if keep_other_draft:
        retained_id, _, _ = edit_draft_and_switch(app, window, bridge, project)
    deleted = create_flow(app, window, bridge, project)
    panel = window.flow_panel
    panel.add_node(QPointF(40, 60))
    panel.prompt_edit.setPlainText('删除前尚未保存的提示')
    assert deleted['id'] in {draft['id'] for draft in panel.pending_drafts()}
    deleted_path = bridge.state_dir / 'flows' / f'{deleted["id"]}.json'
    assert deleted_path.exists()

    # 消息框返回值只模拟用户选择；删除和保存仍经过真实运行时与磁盘。
    monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Yes)
    assert window.delete_button.isEnabled()
    window._delete()
    wait(app, lambda: window.state.get('selected_flow') is None
         and any(action == 'flow_delete' and result.get('flow_id') == deleted['id']
                 for action, result in replies))
    assert not deleted_path.exists()
    assert all(row['id'] != deleted['id'] for row in window.state['flows'])
    assert {draft['id'] for draft in panel.pending_drafts()} == ({retained_id} if retained_id else set())
    assert not errors

    monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Save)
    window.close()
    wait(app, lambda: not bridge.is_running and not bridge.worker_alive and not window.isVisible(), 15)
    assert not errors
    saved = [result for action, result in replies if action == 'flow_save']
    assert all(not result.get('error') for result in saved)
    assert {result['id'] for result in saved} == ({retained_id} if retained_id else set())
    assert not deleted_path.exists()
    if retained_id:
        stored = json.loads((bridge.state_dir / 'flows' / f'{retained_id}.json').read_text())
        assert stored['handoff'] == '保留的初始交接'
        assert [node['prompt'] for node in stored['nodes']] == ['保存的初始提示', '后续验证提示']
        assert len(stored['edges']) == 1
    assert calls == []
