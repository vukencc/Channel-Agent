"""会话目录选择不会在普通设置保存时隐式改写工作区。"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC = Path(__file__).parents[3] / 'src/agent_frontend/web/static'


def test_workspace_settings_save_preserves_root_and_supports_explicit_reset():
    node = shutil.which('node') or shutil.which('nodejs')
    if not node:
        pytest.skip('需要 Linux Node 验证会话设置行为')
    script = (STATIC / 'app.js').read_text()
    save = re.search(r'  async function saveSettings\(.*?\n  \}', script, re.S).group()
    result = subprocess.run([node, '-e', save + r'''
const assert = require('node:assert/strict');
const controls = new Map();
const $ = id => { if (!controls.has(id)) controls.set(id, {value:'',disabled:false,close(){},querySelectorAll(){return[];}}); return controls.get(id); };
const state = {settingsId:'s',activeId:null,settingsAllTools:true,sessions:new Map([['s',{permission_policy:'smart',workspace_custom:true,workspace_path:'/tmp/project'}]])};
let submitted;
const jsonApi = async (_, options) => {submitted=options.body; return state.sessions.get('s');};
const sessionPath=id=>id, showError=()=>{},renderSessions=()=>{},renderTabs=()=>{},toast=()=>{};
$('settings-policy').value='smart';
(async()=>{
  $('settings-project-path').value='/tmp/project';
  await saveSettings({preventDefault(){}});
  assert.equal(Object.hasOwn(submitted,'workspace_path'),false);
  $('settings-project-path').value='/tmp/other';
  await saveSettings({preventDefault(){}});
  assert.equal(submitted.workspace_path,'/tmp/other');
  $('settings-project-path').value='';
  await saveSettings({preventDefault(){}});
  assert.equal(submitted.workspace_path,null);
  $('settings-project-path').disabled=true;
  await saveSettings({preventDefault(){}});
  assert.equal(Object.hasOwn(submitted,'workspace_path'),false);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
