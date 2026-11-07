import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_frontend.desktop import files as desktop_files
from agent_frontend.web import files as web_files


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 目录句柄行为')
@pytest.mark.parametrize('module', [desktop_files, web_files])
def test_windows_directory_scan_uses_pinned_path_and_skips_reparse(tmp_path, monkeypatch, module):
    @contextmanager
    def opened(*args, **kwargs):
        yield 123
    class Entry:
        name = 'junction'
        def is_symlink(self):
            return False
        def stat(self, **kwargs):
            return SimpleNamespace(st_mode=0o040755, st_size=0, st_file_attributes=0x400)
    class Scan:
        def __enter__(self): return iter([Entry()])
        def __exit__(self, *args): pass
    def scandir(path):
        assert path == tmp_path
        return Scan()
    monkeypatch.setattr(module, 'opened_path', opened)
    monkeypatch.setattr(module.os, 'scandir', scandir)

    if module is desktop_files:
        result = module.preview(tmp_path, '.', directory=True)['entries']
    else:
        result = module.directory_entries(tmp_path, tmp_path)
    assert result == []


def test_web_flow_windows_and_linux_path_syntax():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js 不可用')
    script = r'''
const {flowProjectInfo} = require(process.argv[1]);
const cases = [
  ['win32', 'C:\\project', true], ['win32', 'C:/project', true],
  ['win32', 'C:project', false], ['win32', '\\\\server\\share', false],
  ['win32', 'C:\\project\\..\\other', false],
  ['linux', '/project', true], ['linux', '//server/share', false],
  ['linux', '/project/../other', false], ['linux', 'C:\\project', false],
];
for (const [platform, path, valid] of cases) {
  const result = flowProjectInfo({nodes: [{id: 'n'}], workspace_path: path}, 'n', platform);
  if (Boolean(result.path) !== valid) throw new Error(JSON.stringify([platform, path, result]));
}
'''
    source = Path(__file__).resolve().parents[2] / 'agent_frontend' / 'web' / 'static' / 'flow.js'
    subprocess.run([node, '-e', script, str(source)], check=True, capture_output=True, text=True)
