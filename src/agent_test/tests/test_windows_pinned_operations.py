"""受控验证 Windows 操作内目录句柄的复用和释放。"""
import ctypes
import importlib.util
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import types

import pytest


@pytest.fixture
def windows_files(monkeypatch):
    class Function:
        def __call__(self, *args):
            return 1
    class Kernel:
        def __getattr__(self, name):
            return Function()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Kernel(), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(open_osfhandle=lambda handle, flags: handle))
    source = Path(__file__).parents[2] / "agent_core/core/windows_files.py"
    spec = importlib.util.spec_from_file_location("controlled_windows_files", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "Path", PureWindowsPath)
    monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
    monkeypatch.setattr(os, "fstat", lambda fd: types.SimpleNamespace(st_mode=stat.S_IFREG))
    monkeypatch.setattr(os, "close", lambda fd: None)
    return module


def test_reuses_only_ancestors_with_fresh_guards_and_closes(windows_files, monkeypatch):
    module = windows_files
    opened, closed, checked = [], [], []
    def open_path(path, **options):
        handle = len(opened) + 1
        opened.append((str(path), handle))
        return handle, (1, handle)
    def info(handle, pointer):
        checked.append(handle)
        pointer._obj.attributes = 0x10
        return True
    monkeypatch.setattr(module, "_open", open_path)
    monkeypatch.setattr(module, "_info", info)
    monkeypatch.setattr(module, "_identity", lambda handle: (1, handle))
    monkeypatch.setattr(module, "_close", closed.append)
    root = PureWindowsPath("C:/state/flows")
    with module.pinned_scope(root):
        for _ in range(2):
            with module.opened_path(root, root / "file.json"):
                pass
        assert len(opened) == 5  # 三个祖先只打开一次，叶文件每次打开
        assert len(checked) == 3
        assert closed == []
    assert set(closed) == {1, 2, 3}


@pytest.mark.parametrize('changed', ['reparse', 'identity'])
def test_cached_ancestor_change_fails_closed_and_releases(windows_files, monkeypatch, changed):
    module = windows_files
    closed = []
    counter = iter(range(1, 20))
    monkeypatch.setattr(module, '_open', lambda path, **kwargs: ((handle := next(counter)), (1, handle)))
    monkeypatch.setattr(module, '_close', closed.append)
    def info(handle, pointer):
        pointer._obj.attributes = 0x410 if changed == 'reparse' else 0x10
        return True
    monkeypatch.setattr(module, '_info', info)
    monkeypatch.setattr(module, '_identity', lambda handle: (2, handle))
    root = PureWindowsPath('C:/state/flows')
    with pytest.raises(PermissionError):
        with module.pinned_scope(root):
            with module.opened_path(root, root / 'file.json'):
                pass
            with module.opened_path(root, root / 'file.json'):
                pass
    assert set(closed) == {1, 2, 3}


def test_nested_scope_and_later_operation_do_not_leak(windows_files, monkeypatch):
    module = windows_files
    opened, closed = [], []
    def open_path(path, **kwargs):
        handle = len(opened) + 1
        opened.append(handle)
        return handle, (1, handle)
    def info(handle, pointer):
        pointer._obj.attributes = 0x10
        return True
    monkeypatch.setattr(module, '_open', open_path)
    monkeypatch.setattr(module, '_close', closed.append)
    monkeypatch.setattr(module, '_info', info)
    monkeypatch.setattr(module, '_identity', lambda handle: (1, handle))
    root = PureWindowsPath('C:/state/flows')
    with module.pinned_scope(root):
        with module.opened_path(root, root / 'file.json'):
            pass
        with module.pinned_scope(root):
            with module.opened_path(root, root / 'file.json'):
                pass
        assert not closed
    assert set(closed) == {1, 2, 3}
    with module.pinned_scope(root):
        with module.opened_path(root, root / 'file.json'):
            pass
    assert set(closed) == {1, 2, 3, 6, 7, 8}


def test_unrelated_prefix_is_not_reused(windows_files, monkeypatch):
    module = windows_files
    opened, closed = [], []
    def open_path(path, **kwargs):
        handle = len(opened) + 1
        opened.append(handle)
        return handle, (1, handle)
    monkeypatch.setattr(module, '_open', open_path)
    monkeypatch.setattr(module, '_close', closed.append)
    root = PureWindowsPath('D:/other')
    with module.pinned_scope(PureWindowsPath('C:/state')):
        for _ in range(2):
            with module.opened_path(root, root / 'file.json'):
                pass
        assert set(closed) == {1, 2, 4, 5}
    assert len(opened) == 6


def test_same_drive_sibling_does_not_borrow_cached_ancestors(windows_files, monkeypatch):
    module = windows_files
    opened, closed = [], []
    def open_path(path, **kwargs):
        handle = len(opened) + 1
        opened.append((str(path), handle))
        return handle, (1, handle)
    def info(handle, pointer):
        pointer._obj.attributes = 0x10
        return True
    monkeypatch.setattr(module, '_open', open_path)
    monkeypatch.setattr(module, '_close', closed.append)
    monkeypatch.setattr(module, '_info', info)
    monkeypatch.setattr(module, '_identity', lambda handle: (1, handle))
    root = PureWindowsPath('C:/state/flows')
    sibling = PureWindowsPath('C:/state/projects')
    with module.pinned_scope(root):
        with module.opened_path(root, root / 'file.json'):
            pass
        with module.opened_path(sibling, sibling / 'file.json'):
            pass
        assert len(opened) == 8
        assert set(closed) == {5, 6, 7}
    assert set(closed) == {1, 2, 3, 5, 6, 7}
