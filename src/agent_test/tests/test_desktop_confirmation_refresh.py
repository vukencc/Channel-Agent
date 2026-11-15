"""确认对话只对当前用户决定发出一次绑定请求。"""
import pytest

pytest.importorskip('PySide6.QtWidgets')
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget
from agent_frontend.desktop.window import MainWindow


@pytest.fixture
def owner():
    app = QApplication.instance() or QApplication([])
    class Bridge:
        def __init__(self):
            self.calls = []
            self.stopped = 0
        def request(self, action, **kwargs):
            self.calls.append((action, kwargs))
        def shutdown(self):
            self.stopped += 1
    class Owner(QWidget):
        _show_confirmation = MainWindow._show_confirmation
        _begin_shutdown = MainWindow._begin_shutdown
        def __init__(self):
            super().__init__()
            self.bridge = Bridge()
            self.confirmation_dialog = None
            self._confirmation_key = None
            self.current_session = None
            self.state = {'sessions': []}
            self._closing = False
            self.phase_label = type('Label', (), {'setText': lambda *args: None})()
            self.composer = type('Composer', (), {'setEnabled': lambda *args: None})()
        def _update_buttons(self):
            pass
    window = Owner()
    yield window
    window.close()
    app.processEvents()


def session(identifier, pending):
    return {'id': identifier, 'title': identifier,
            'confirmation': {'id': pending, 'prompt': '受控操作'} if pending else None}


@pytest.mark.parametrize('change', ['expired', 'replaced', 'navigation'])
def test_programmatic_removal_never_sends_old_decision(owner, change):
    owner.current_session = session('one', 'old')
    owner._show_confirmation()
    old = owner.confirmation_dialog
    owner.current_session = {'expired': session('one', None), 'replaced': session('one', 'new'),
                             'navigation': session('two', 'different')}[change]
    owner._show_confirmation()
    assert owner.bridge.calls == []
    old.accept()
    assert owner.bridge.calls == []
    if change != 'expired':
        assert owner.confirmation_dialog is not old
        owner.confirmation_dialog.accept()
        assert owner.bridge.calls == [('decide', {'session_id': owner.current_session['id'],
                                                   'confirmation_id': owner.current_session['confirmation']['id'], 'allowed': True})]


@pytest.mark.parametrize('action,allowed', [('拒绝', False), ('允许本次', True), ('close', False)])
def test_user_decision_sends_once_for_current_key(owner, action, allowed):
    owner.current_session = session('one', 'current')
    owner._show_confirmation()
    dialog = owner.confirmation_dialog
    if action == 'close':
        dialog.close()
    else:
        buttons = dialog.findChild(QDialogButtonBox)
        next(button for button in buttons.buttons() if button.text() == action).click()
    owner._show_confirmation()
    assert owner.bridge.calls == [('decide', {'session_id': 'one', 'confirmation_id': 'current', 'allowed': allowed})]
    assert owner.confirmation_dialog is None


def test_flow_current_node_change_cannot_decide_previous_node(owner):
    first, second = session('first', 'old'), session('second', 'new')
    owner.state = {'sessions': [first, second], 'selected_flow': {'run': {'current_node': 'a', 'nodes': {'a': {'session_id': 'first'}, 'b': {'session_id': 'second'}}}}}
    owner._show_confirmation()
    old = owner.confirmation_dialog
    owner.state['selected_flow']['run']['current_node'] = 'b'
    owner._show_confirmation()
    old.accept()
    assert owner.bridge.calls == []
    owner.confirmation_dialog.reject()
    assert owner.bridge.calls == [('decide', {'session_id': 'second', 'confirmation_id': 'new', 'allowed': False})]


def test_shutdown_dismisses_once_without_sending_stale_decision(owner):
    owner.current_session = session('one', 'current')
    owner._show_confirmation()
    owner._begin_shutdown()
    assert owner.bridge.calls == []
    assert owner.bridge.stopped == 1
    assert owner.confirmation_dialog is None
    owner._show_confirmation()
    assert owner.confirmation_dialog is None
    assert owner.bridge.calls == []
