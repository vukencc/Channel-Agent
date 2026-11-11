"""Working 提示不能遮挡相邻节点的指针操作。"""
from pathlib import Path
import shutil
import subprocess


def test_working_tooltip_does_not_intercept_pointer(tmp_path):
    node = shutil.which('node')
    assert node
    source = Path(__file__).resolve().parents[2] / 'agent_frontend/web/static/working.js'
    script = tmp_path / 'check.cjs'
    script.write_text('''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let src = fs.readFileSync(process.argv[2], 'utf8');
src = src.replace('window.WorkingUI = {', 'window.makeTip = makeTip; window.WorkingUI = {');
global.document = {createElement: () => ({style: {}, setAttribute() {}})};
global.window = {};
vm.runInThisContext(src);
assert.equal(window.makeTip().style.pointerEvents, 'none');
''', encoding='utf-8')
    result = subprocess.run([node, str(script), str(source)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
