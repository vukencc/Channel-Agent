"""无会话时模型管理入口及其快捷命令。"""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest

STATIC = Path(__file__).resolve().parents[2] / 'agent_frontend/web/static'


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.nodes = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.nodes[attrs['id']] = (tag, attrs, tuple(self.stack))
        if tag not in {'input', 'br', 'meta', 'link', 'img', 'path'}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                break


def test_sidebar_model_entry_is_a_keyboard_button_and_command_is_session_independent():
    dom = Elements()
    dom.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
    tag, attrs, parents = dom.nodes['sidebar-models']
    assert tag == 'button' and attrs.get('type') == 'button' and 'disabled' not in attrs
    assert any('sidebar-bottom' in parent.get('class', '').split() for _, parent in parents)
    js = (STATIC / 'app.js').read_text(encoding='utf-8')
    assert '["manage-models", "sidebar-models"].forEach((id) => $(id).addEventListener("click", openModels))' in js
    entry = re.search(r'\{ label: "模型管理"[^\n]+\}', js)
    assert entry and 'requireSession' not in entry.group() and 'run: openModels' in entry.group()
    assert '$("models-dialog").addEventListener("close", () => { cancelModelTest(); $("model-api-key").value = ""; });' in js


def test_sidebar_and_command_open_same_dialog_without_session():
    node = shutil.which('node') or shutil.which('nodejs')
    if not node:
        pytest.skip('Node is required')
    js = (STATIC / 'app.js').read_text(encoding='utf-8')
    match = re.search(r'  async function openModels\(\).*?\n  \}', js, re.S)
    assert match
    load = re.search(r'  async function loadModels\(\).*?\n  \}', js, re.S)
    assert load
    entry = re.search(r'\{ label: "模型管理"[^\n]+\}', js)
    assert entry
    script = load.group() + match.group() + r'''
const assert=require('node:assert/strict');
let resets=0, loads=0, dialog={open:false, showModal(){this.open=true;}};
const list={replaceChildren(node){this.node=node;}};
const $=id=>id==='models-dialog'?dialog:list;
const element=(tag, cls, text)=>({text});
function resetModelForm(){resets++;}
async function jsonApi(path){assert.equal(path,'/api/models'); loads++; return {models:[{id:'one',name:'已保存连接',model:'m',proxy_mode:'environment'}]};}
function renderModels(){list.replaceChildren({text:state.modelLibrary[0].name});}
const window={};
function showError(){throw Error('unexpected error');}
const state={activeId:null};
const command=''' + entry.group() + r''';
(async()=>{
  await openModels(); assert.equal(dialog.open,true); assert.equal(list.node.text,'已保存连接');
  dialog.open=false; await command.run();
  assert.equal(dialog.open,true); assert.equal(loads,2); assert.equal(resets,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node], input=script, text=True, encoding='utf-8', capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
