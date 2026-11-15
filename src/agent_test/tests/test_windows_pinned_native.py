"""真实 Windows 内核回归；无模型、回放或 UI 夹具，不修改宿主权限配置。"""
import asyncio
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='需要真实 Windows 文件句柄和 NTFS')


@pytest.mark.parametrize('failure', [None, RuntimeError, asyncio.CancelledError, TimeoutError],
                         ids=['normal', 'exception', 'cancellation-unwind', 'timeout-unwind'])
def test_pinned_ancestors_block_rename_until_scope_unwinds(tmp_path: Path, failure):
    """叶句柄关闭后祖先仍被固定；同步异常展开释放句柄，不模拟调度器超时。"""
    from agent_core.core.windows_files import opened_path, pinned_scope

    root = tmp_path / 'root'
    prefix = root / 'state'
    prefix.mkdir(parents=True)
    leaf = prefix / 'file'
    leaf.write_bytes(b'owned-fixture')
    expected = pytest.raises(failure) if failure else nullcontext()
    with expected:
        with pinned_scope(prefix):
            with opened_path(prefix, leaf) as fd:
                assert os.read(fd, 64) == b'owned-fixture'
            # 文件已关闭；两级祖先必须仍由真实句柄禁止重命名。
            for ancestor in (prefix, root):
                with pytest.raises(OSError) as error:
                    ancestor.rename(ancestor.with_name(ancestor.name + '-moved'))
                assert error.value.winerror == 32
            if failure:
                raise failure('受控异常展开')
    moved_prefix = root / 'state-moved'
    prefix.rename(moved_prefix)
    moved_root = tmp_path / 'root-moved'
    root.rename(moved_root)
    assert (moved_root / moved_prefix.name / 'file').read_bytes() == b'owned-fixture'


def test_hardlink_added_after_first_scoped_read_is_rejected(tmp_path: Path):
    """缓存仅限祖先，叶文件链接数必须在第二次打开时重新验证。"""
    from agent_core.core.windows_files import opened_path, pinned_scope

    root = tmp_path / 'root'
    root.mkdir()
    leaf = root / 'file'
    outside = tmp_path / 'outside-owned-file'
    leaf.write_bytes(b'owned-fixture')
    with pinned_scope(root):
        with opened_path(root, leaf) as fd:
            assert os.read(fd, 64) == b'owned-fixture'
        os.link(leaf, outside)
        assert leaf.stat().st_nlink == outside.stat().st_nlink == 2
        with pytest.raises(PermissionError, match='硬链接'):
            with opened_path(root, leaf):
                pytest.fail('新增硬链接后不应交付文件描述符')
    assert outside.read_bytes() == leaf.read_bytes() == b'owned-fixture'


def test_same_drive_prefix_sibling_gets_full_checks_and_is_not_pinned(tmp_path: Path, monkeypatch):
    """实际系统调用旁记录打开次数，名称前缀相同也不能复用兄弟根目录。"""
    from agent_core.core import windows_files as files

    root = tmp_path / 'state'
    sibling = tmp_path / 'state-other'
    root.mkdir()
    sibling.mkdir()
    (root / 'file').write_bytes(b'inside')
    (sibling / 'file').write_bytes(b'sibling')
    opened = []
    native_open = files._open

    def recording_open(path, **options):
        opened.append(path)
        return native_open(path, **options)

    monkeypatch.setattr(files, '_open', recording_open)
    with files.pinned_scope(root):
        with files.opened_path(root, root / 'file'):
            pass
        with pytest.raises(PermissionError, match='不属于'):
            with files.opened_path(root, sibling / 'file'):
                pytest.fail('兄弟目录不是已授权根的子目录')
        for _ in range(2):
            before = len(opened)
            with files.opened_path(sibling, sibling / 'file') as fd:
                assert os.read(fd, 64) == b'sibling'
            assert opened[before:] == [Path(sibling.anchor), *reversed(sibling.parents[:-1]),
                                      sibling, sibling / 'file']
            moved = tmp_path / 'sibling-moved'
            sibling.rename(moved)
            moved.rename(sibling)
    assert opened.count(sibling) == 2


def test_new_junction_below_pinned_prefix_is_rejected(tmp_path: Path):
    """复用现有 mklink /J 方式；这是替换未缓存的子目录，不是原位修改重解析点。"""
    from agent_core.core.windows_files import opened_path, pinned_scope

    root = tmp_path / 'root'
    child = root / 'child'
    child.mkdir(parents=True)
    outside = tmp_path / 'outside-owned-directory'
    outside.mkdir()
    (child / 'file').write_bytes(b'inside')
    (outside / 'file').write_bytes(b'outside')
    with pinned_scope(root):
        with opened_path(root, child / 'file') as fd:
            assert os.read(fd, 64) == b'inside'
        (child / 'file').unlink()
        child.rmdir()
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(child), str(outside)],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        try:
            with pytest.raises(PermissionError):
                with opened_path(root, child / 'file'):
                    pytest.fail('新增目录联接不应交付文件描述符')
            assert (outside / 'file').read_bytes() == b'outside'
        finally:
            # 仅移除已知目录联接，不递归访问其目标。
            os.rmdir(child)


def test_new_scope_observes_revoked_directory_dacl(tmp_path: Path):
    """复用已有 ACL 辅助器；旧 HANDLE 权限固定，不宣称操作中重查 DACL。"""
    from test_windows_portable_acl import NativeAcl
    from agent_core.core.windows_files import opened_path, pinned_scope

    acl = NativeAcl()
    root = tmp_path / 'root'
    root.mkdir()
    leaf = root / 'file'
    leaf.write_bytes(b'owned-fixture')
    with pinned_scope(root):
        with opened_path(root, leaf) as fd:
            assert os.read(fd, 64) == b'owned-fixture'
    try:
        # 只拒绝夹具目录的读取，保留 WRITE_DAC 供 finally 恢复。
        acl.apply(root, f'D:P(D;;GR;;;{acl.current_sid})(A;;FA;;;SY)(A;;FA;;;{acl.current_sid})')
        with pytest.raises(PermissionError) as control:
            with os.scandir(root) as entries:
                list(entries)
        assert control.value.winerror == 5
        with pytest.raises(PermissionError) as denied:
            with pinned_scope(root):
                with opened_path(root, leaf):
                    pytest.fail('新操作必须受撤销后的目录读取权限约束')
        assert denied.value.winerror == 5
    finally:
        acl.apply(root, f'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{acl.current_sid})')
    with pinned_scope(root):
        with opened_path(root, leaf) as fd:
            assert os.read(fd, 64) == b'owned-fixture'
