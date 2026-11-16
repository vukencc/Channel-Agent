import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QWidget
import json
from pathlib import Path
import re
import shutil
import subprocess

@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])

from agent_frontend.desktop.window import MainWindow, ModelDialog
from agent_frontend.desktop.flow_designer import FlowDesigner


def test_proxy_setting_visible_without_opening_advanced(qapp):
    dialog = ModelDialog()
    dialog.show()
    assert dialog.proxy_mode.isVisible()
    assert "环境" in dialog.proxy_mode.currentText()
    dialog.close()


def test_designer_wait_reports_elapsed_time(qapp, monkeypatch):
    import agent_frontend.desktop.flow_designer as module
    now = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    panel = FlowDesigner()
    panel.input.setPlainText("设计流程")
    panel.submit()
    now[0] = 22.0
    panel._animate_status()
    assert "12" in panel.status.text()
    assert "复杂" in panel.status.text()
    panel.cancel()


def test_model_test_cancel_and_late_result_are_scoped_to_dialog(qapp):
    class Bridge(QObject):
        state_changed = Signal(str)
        finished = Signal(str, str)

        def __init__(self):
            super().__init__()
            self.calls = []

        def request(self, action, **kwargs):
            self.calls.append((action, kwargs))

    class Owner(QWidget):
        _models = MainWindow._models

        def __init__(self):
            super().__init__()
            self.bridge = Bridge()
            self.state = {'models': [{'id': 'one', 'name': '示例', 'model': 'm', 'read_only': False, 'proxy_mode': 'environment'}]}

    owner = Owner()
    def button(dialog, text):
        return next(b for b in dialog.findChildren(QPushButton) if b.text() == text)

    def start_then_close():
        dialog = owner.findChild(QDialog)
        button(dialog, '测试连接').click()
        assert not button(dialog, '测试连接').isEnabled()
        dialog.accept()

    QTimer.singleShot(0, start_then_close)
    owner._models()
    first = owner.bridge.calls[0][1]
    assert owner.bridge.calls[1] == ('model_test_cancel', {'request_id': first['request_id']})

    def check_late_result():
        dialog = owner.findChildren(QDialog)[-1]
        button(dialog, '测试连接').click()
        second = owner.bridge.calls[-1][1]
        assert first['request_id'] != second['request_id']
        owner.bridge.finished.emit('model_test_connection', json.dumps({**first, 'ok': True, 'elapsed_s': 1}))
        status = dialog.findChild(QLabel, 'modelTestStatus')
        assert '正在测试' in status.text()
        owner.bridge.finished.emit('model_test_connection', json.dumps({**second, 'ok': False, 'proxy_mode': 'windows_system', 'elapsed_s': 2.5, 'message': '失败'}))
        assert '连接失败' in status.text() and 'Windows 系统代理' in status.text() and '2.5' in status.text()
        assert button(dialog, '测试连接').isEnabled()
        dialog.accept()

    QTimer.singleShot(0, check_late_result)
    owner._models()


def test_web_model_test_close_sends_matching_cancel_and_ignores_late_result():
    node = shutil.which('node') or shutil.which('nodejs')
    if not node:
        pytest.skip('Node is required')
    source = (Path(__file__).resolve().parents[2] / 'agent_frontend/web/static/app.js').read_text(encoding='utf-8')
    def function(name):
        match = re.search(r'  (?:async )?function ' + name + r'\(.*?\n  \}', source, re.S)
        assert match
        return match.group()

    script = function('cancelModelTest') + function('renderModels') + r'''
const assert = require('node:assert/strict');
let modelTestController = null, calls = [], resolvers = [], next = 0, allButtons = [];
const crypto = { randomUUID: () => `id-${++next}` };
const performance = { now: () => 1000 };
class Node {
  constructor(text='') { this.textContent=text; this.children=[]; this.dataset={}; this.handlers={}; this.disabled=false; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute() {}
  addEventListener(name, fn) { this.handlers[name]=fn; }
  replaceChildren(...nodes) { this.children=nodes; }
}
const element = (tag, cls, text) => { const n=new Node(text); if(tag==='button') allButtons.push(n); return n; };
const list = new Node(), $ = () => list;
const document = {createDocumentFragment: () => new Node(), querySelectorAll: () => allButtons.filter(b => b.dataset.modelTest)};
const state = {modelLibrary:[{id:'model / 1', name:'model', model:'m', proxy_mode:'environment', editable:false}]};
function jsonApi(path, options) {
  calls.push({path, options, originalAborted: calls[0]?.options.signal?.aborted});
  if (path.endsWith('/cancel')) return Promise.resolve({});
  return new Promise(resolve => resolvers.push(resolve));
}
async function main() {
  renderModels();
  assert.equal(allButtons.some(b => b.textContent === '选择'),false);
  const firstButton=allButtons.find(b => b.dataset.modelTest);
  const firstRun=firstButton.handlers.click();
  assert.equal(firstButton.disabled,true);
  assert.equal(calls[0].path,'/api/models/model%20%2F%201/test-connection');
  assert.equal(calls[0].options.body.request_id,'id1');
  cancelModelTest();
  assert.equal(calls[1].path,'/api/models/model%20%2F%201/test-connection/cancel');
  assert.deepEqual(calls[1].options.body,{request_id:'id1'});
  assert.equal(calls[1].options.keepalive,true);
  assert.equal(calls[1].options.signal,undefined);
  assert.equal(calls[1].originalAborted,false);
  assert.equal(calls[0].options.signal.aborted,true);
  allButtons=[]; renderModels();
  const secondButton=allButtons.find(b => b.dataset.modelTest);
  const secondRun=secondButton.handlers.click();
  assert.equal(calls[2].options.body.request_id,'id2');
  resolvers[0]({ok:true, elapsed_s:1}); await firstRun;
  assert.equal(secondButton.disabled,true);
  cancelModelTest();
  assert.deepEqual(calls[3].options.body,{request_id:'id2'});
  resolvers[1]({ok:true, elapsed_s:1}); await secondRun;
}
main().catch(error => { console.error(error); process.exitCode=1; });
'''
    completed = subprocess.run([node], input=script, text=True, encoding='utf-8', capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
