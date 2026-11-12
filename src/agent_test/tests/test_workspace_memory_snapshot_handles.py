"""共享快照允许原子替换目标文件，但不放开祖先目录与普通写入句柄。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from agent_core.core.files import directory_identity, opened_path
from agent_core.core.workspace_memory import WorkspaceMemoryDelta, WorkspaceMemoryStore


@pytest.mark.parametrize('options', [{'directory': True}, {'writable': True}])
def test_replaceable_handle_rejects_directory_and_write(tmp_path, options):
    with pytest.raises(ValueError):
        with opened_path(tmp_path, tmp_path / 'missing', allow_replace=True, **options):
            pytest.fail('无效句柄不得打开')
    assert not list(tmp_path.iterdir())


def test_replaceable_handle_preserves_root_identity_and_hardlink_checks(tmp_path):
    target = tmp_path / 'file'
    target.write_text('allowed', encoding='utf-8')
    identity = directory_identity(tmp_path)
    with pytest.raises(PermissionError):
        with opened_path(tmp_path, target, allow_replace=True,
                         expected_identity=(identity[0], identity[1] + 1)):
            pytest.fail('根身份变化不得读取')
    other = tmp_path / 'hardlink'
    other.hardlink_to(target)
    with pytest.raises(PermissionError):
        with opened_path(tmp_path, target, allow_replace=True, expected_identity=identity):
            pytest.fail('多硬链接不得读取')


def test_replaceable_handle_rejects_link_and_outside(tmp_path, symlink_capable):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('outside', encoding='utf-8')
    link = root / 'link'
    link.symlink_to(outside)
    for target in (link, outside):
        with pytest.raises(PermissionError):
            with opened_path(root, target, allow_replace=True, expected_identity=directory_identity(root)):
                pytest.fail('不得越界读取')


@pytest.mark.skipif(os.name != 'nt', reason='Windows 删除共享句柄回归')
@pytest.mark.parametrize('allow_replace', [False, True])
def test_windows_held_snapshot_and_other_process_append(tmp_path, allow_replace):
    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    store.append(WorkspaceMemoryDelta(progress='first'), update_id='one', outcome='idle',
                 cancelled=threading.Event(), authorize=lambda: True, audit=lambda *args, **kwargs: None)
    script = '''
import sys,threading
from pathlib import Path
from agent_core.core.files import directory_identity
from agent_core.core.workspace_memory import WorkspaceMemoryStore,WorkspaceMemoryDelta
root=Path(sys.argv[1])
store=WorkspaceMemoryStore(root,directory_identity(root))
store.append(WorkspaceMemoryDelta(progress="second"),update_id="two",outcome="idle",
             cancelled=threading.Event(),authorize=lambda:True,audit=lambda *args,**kwargs:None)
'''
    with opened_path(tmp_path, store.path, allow_replace=allow_replace,
                     expected_identity=directory_identity(tmp_path)) as fd:
        result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                                capture_output=True, timeout=15,
                                env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'})
        assert (result.returncode == 0) == allow_replace, result.stderr.decode(errors='replace')
        assert json.loads(os.read(fd, 131072))['revision'] == 1
        with pytest.raises(OSError):
            tmp_path.rename(tmp_path.with_name(tmp_path.name + '-moved'))
    assert store.read().revision == (2 if allow_replace else 1)
