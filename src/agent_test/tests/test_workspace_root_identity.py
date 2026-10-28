"""用户授权的外部工作区不能被路径替换重新定向。"""
from threading import Event

import pytest

from agent_core.tools.sandbox import SandboxError, ToolContext, sandbox_root, tool_context


def _context(path, audit_path, identity):
    return ToolContext(path, audit_path, lambda *_: True, Event(), root_identity=identity)


def test_external_workspace_disappearance_does_not_recreate_directory(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    identity = (root.stat().st_dev, root.stat().st_ino)
    root.rmdir()
    with tool_context(_context(root, tmp_path / 'audit.jsonl', identity)):
        with pytest.raises(SandboxError):
            sandbox_root()
    assert not root.exists()


@pytest.mark.parametrize('replacement', ['symlink', 'directory'])
def test_external_workspace_replacement_is_rejected(tmp_path, replacement):
    root, original, victim = (tmp_path / name for name in ('project', 'original', 'victim'))
    root.mkdir()
    victim.mkdir()
    (victim / 'keep.txt').write_text('保留')
    identity = (root.stat().st_dev, root.stat().st_ino)
    root.rename(original)
    if replacement == 'symlink':
        root.symlink_to(victim, target_is_directory=True)
    else:
        root.mkdir()
    with tool_context(_context(root, tmp_path / 'audit.jsonl', identity)):
        with pytest.raises(SandboxError):
            sandbox_root()
    assert (victim / 'keep.txt').read_text() == '保留'


def test_external_workspace_keeps_authorized_identity(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    identity = (root.stat().st_dev, root.stat().st_ino)
    with tool_context(_context(root, tmp_path / 'audit.jsonl', identity)):
        assert sandbox_root() == root
