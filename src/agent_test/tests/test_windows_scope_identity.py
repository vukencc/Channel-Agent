"""Windows 原生句柄回归；真实临时文件，不访问既有用户数据。"""
import ctypes
import os
from collections import Counter

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows 原生句柄专用回归')


@pytest.fixture
def native_scope(tmp_path, monkeypatch):
    from agent_core.core import windows_files as windows

    root = tmp_path / 'workspace'
    nested = root / 'nested'
    nested.mkdir(parents=True)
    for path in (root / 'one.txt', root / 'two.txt', nested / 'child.txt'):
        path.write_bytes(b'isolated-content')
    original_open, original_identity, original_info, original_close = (
        windows._open, windows._identity, windows._info, windows._close)
    identities, attributes, closed = Counter(), Counter(), Counter()
    opened = []
    identity_events = []

    def identity(handle):
        identities[handle] += 1
        identity_events.append(handle)
        return original_identity(handle)

    def info(handle, pointer):
        attributes[handle] += 1
        return original_info(handle, pointer)

    def open_path(path, **kwargs):
        before = len(identity_events)
        result = original_open(path, **kwargs)
        opened.append((path, result[0], {**kwargs, 'identity_queries': len(identity_events) - before}))
        return result

    def close(handle):
        closed[handle] += 1
        return original_close(handle)

    monkeypatch.setattr(windows, '_identity', identity)
    monkeypatch.setattr(windows, '_info', info)
    monkeypatch.setattr(windows, '_open', open_path)
    monkeypatch.setattr(windows, '_close', close)
    return windows, root, opened, identities, attributes, closed


def _read(windows, root, path):
    with windows.opened_path(root, path) as descriptor:
        assert os.read(descriptor, 64) == b'isolated-content'


def test_cached_ancestor_identity_queried_once_but_attributes_rechecked(native_scope):
    windows, root, opened, identities, attributes, closed = native_scope
    with windows.pinned_scope(root):
        _read(windows, root, root / 'one.txt')
        _read(windows, root, root / 'two.txt')
        ancestor = next(handle for path, handle, _ in opened if path == root)
        assert attributes[ancestor] == 2
        assert identities[ancestor] == 1
        assert closed[ancestor] == 0
        assert len([row for row in opened if row[0] in (root / 'one.txt', root / 'two.txt')]) == 2
    assert closed[ancestor] == 1
    assert windows._scopes.stack == []


@pytest.mark.parametrize('changed_attributes', [0x10 | 0x400, 0])
def test_cached_ancestor_reparse_or_type_change_rejected_and_cleaned(native_scope, monkeypatch, changed_attributes):
    windows, root, opened, identities, attributes, closed = native_scope
    with windows.pinned_scope(root):
        _read(windows, root, root / 'one.txt')
        ancestor = next(handle for path, handle, _ in opened if path == root)
        original_info = windows._info

        def changed_info(handle, pointer):
            success = original_info(handle, pointer)
            if success and handle == ancestor:
                ctypes.cast(pointer, ctypes.POINTER(windows._FileInfo)).contents.attributes = changed_attributes
            return success

        monkeypatch.setattr(windows, '_info', changed_info)
        with pytest.raises(PermissionError, match='路径权限或类型'):
            _read(windows, root, root / 'two.txt')
        assert not any(path == root / 'two.txt' for path, _, _ in opened)
        assert closed[ancestor] == 0
    assert closed[ancestor] == 1
    assert windows._scopes.stack == []


def test_fresh_scope_reopens_and_requeries_ancestor_identity(native_scope):
    windows, root, opened, identities, attributes, closed = native_scope
    for name in ('one.txt', 'two.txt'):
        before = sum(identities.values())
        with windows.pinned_scope(root):
            _read(windows, root, root / name)
            ancestor = [handle for path, handle, _ in opened if path == root][-1]
            assert sum(identities.values()) > before
        assert closed[ancestor] >= 1
    assert len([row for row in opened if row[0] == root]) == 2
    assert all(options['identity_queries'] == 1 for path, _, options in opened if path == root)
    assert windows._scopes.stack == []


def test_nested_shared_scope_keeps_parent_handles_alive_until_outer_exit(native_scope):
    windows, root, opened, identities, attributes, closed = native_scope
    with windows.pinned_scope(root):
        _read(windows, root, root / 'one.txt')
        ancestor = next(handle for path, handle, _ in opened if path == root)
        with windows.pinned_scope(root / 'nested'):
            _read(windows, root, root / 'nested' / 'child.txt')
        assert closed[ancestor] == 0
        _read(windows, root, root / 'two.txt')
        assert attributes[ancestor] == 3
        assert identities[ancestor] == 1
    assert closed[ancestor] == 1
    assert windows._scopes.stack == []
