"""Windows 历史目录只在同步操作内复用祖先，叶文件始终重新检查。"""
from collections import Counter
import json
import os
from types import SimpleNamespace

import pytest

from agent_core.core.history_context import HistoryContext
from agent_core.core.storage import SessionStore


@pytest.mark.skipif(os.name != 'nt', reason='Windows 祖先句柄、硬链接及删除共享语义')
def test_history_scope_reuses_ancestors_but_reopens_files(tmp_path, monkeypatch):
    from agent_core.core import windows_files

    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspaces')
    session = SimpleNamespace(id='a' * 32)
    service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
    try:
        with service._directories(session, create=True) as (folder, _, _, _):
            pass
        leaf = folder / 'chunks' / 'sample.json'
        store.atomic_write(leaf, json.dumps({'synthetic': True}))
        calls = Counter()
        original = windows_files._open

        def counted(path, **kwargs):
            calls[(path, kwargs['directory'])] += 1
            return original(path, **kwargs)
        monkeypatch.setattr(windows_files, '_open', counted)
        ancestor = (store.root.parent, True)
        replacement = folder.with_name('context-moved')
        assert folder.resolve().is_relative_to(tmp_path.resolve())
        assert replacement.absolute().is_relative_to(tmp_path.resolve())
        assert not replacement.exists()
        with service._directories(session) as (_, _, _, chunks_fd):
            before = calls[ancestor]
            for _ in range(3):
                assert service._read_json(leaf, chunks_fd) == {'synthetic': True}
            assert calls[ancestor] == before, '同一同步操作不应反复打开相同祖先'
            assert calls[(leaf, False)] == 3, '叶文件不能复用旧句柄'
            alias = tmp_path / 'outside-alias.json'
            os.link(leaf, alias)
            try:
                with pytest.raises(PermissionError):
                    service._read_json(leaf, chunks_fd)
            finally:
                alias.unlink()
            with pytest.raises(OSError):
                folder.rename(replacement)
        # 退出作用域释放全部目录句柄；下一次操作必须重新取得祖先身份。
        folder.rename(replacement)
        replacement.rename(folder)
        before = calls[ancestor]
        with service._directories(session) as (_, _, _, chunks_fd):
            assert calls[ancestor] > before
            assert service._read_json(leaf, chunks_fd) == {'synthetic': True}
    finally:
        store.close()
