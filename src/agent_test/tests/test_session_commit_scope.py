"""会话提交的同步作用域和 Windows 祖先句柄复用。"""
import json
import os
from contextlib import contextmanager
from pathlib import PureWindowsPath
import sys
import types

import pytest

from agent_core.core import storage
from test_windows_pinned_operations import windows_files


def test_intermediate_session_directory_reused_but_leaves_reopened(windows_files, monkeypatch):
    module = windows_files
    opened, closed, checked = [], [], []
    def open_path(path, **kwargs):
        handle = len(opened) + 1
        opened.append((path, handle))
        return handle, (1, handle)
    def info(handle, pointer):
        checked.append(handle)
        pointer._obj.attributes = 0x10
        return True
    monkeypatch.setattr(module, '_open', open_path)
    monkeypatch.setattr(module, '_close', closed.append)
    monkeypatch.setattr(module, '_info', info)
    monkeypatch.setattr(module, '_identity', lambda handle: (1, handle))
    root = PureWindowsPath('C:/state')
    session = root / ('a' * 32)
    with module.pinned_scope(session):
        for name in ('session.json', 'index.json'):
            with module.opened_path(root, session / name):
                pass
        assert [path for path, _ in opened].count(session) == 1
        assert [path for path, _ in opened].count(session / 'session.json') == 1
        assert [path for path, _ in opened].count(session / 'index.json') == 1
        assert len(checked) == 3
        assert not closed
    assert len(closed) == 3


class WindowsOS:
    name = 'nt'
    def __getattr__(self, name):
        return getattr(os, name)


@pytest.mark.parametrize('fail', [False, True])
def test_commit_scope_closes_and_failure_does_not_publish(tmp_path, monkeypatch, fail):
    store = storage.SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    record = store.new_record('title', 'system')
    directory = store.directory(record['id'])
    store.save(record)
    previous = (directory / 'session.json').read_bytes()
    record['messages'].append({'role': 'user', 'content': 'next'})
    events = []
    @contextmanager
    def scope(prefix):
        events.append(('enter', prefix))
        try:
            yield
        finally:
            events.append(('exit', prefix))
    original_write = store.atomic_write
    def write(path, text):
        assert events == [('enter', directory)]
        if fail:
            raise OSError('controlled failure before index publication')
        original_write(path, text)
    scoped_module = types.SimpleNamespace(pinned_scope=scope)
    if os.name == 'nt':
        # 保留真实 Windows 原子写盘后端；这里只替换被测作用域入口。
        from agent_core.core.windows_files import atomic_replace
        scoped_module.atomic_replace = atomic_replace
    monkeypatch.setitem(sys.modules, 'agent_core.core.windows_files', scoped_module)
    monkeypatch.setattr(storage, 'os', WindowsOS())
    monkeypatch.setattr(store, 'atomic_write', write)
    try:
        if fail:
            with pytest.raises(OSError, match='controlled failure'):
                store.save(record)
            assert (directory / 'session.json').read_bytes() == previous
        else:
            store.save(record)
            assert json.loads((directory / 'session.json').read_text())['message_count'] == 2
        assert events == [('enter', directory), ('exit', directory)]
    finally:
        monkeypatch.setattr(storage, 'os', os)
        store.close()


@pytest.mark.skipif(os.name == 'nt', reason='仅验证 Linux 提交分支，Windows 分支另有覆盖')
def test_linux_commit_does_not_enter_windows_scope(tmp_path, monkeypatch):
    store = storage.SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    monkeypatch.setitem(sys.modules, 'agent_core.core.windows_files', types.SimpleNamespace(
        pinned_scope=lambda path: pytest.fail('Linux must not enter Windows scope')))
    try:
        store.save(store.new_record('title', 'system'))
    finally:
        store.close()
