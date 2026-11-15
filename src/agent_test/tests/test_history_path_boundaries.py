"""结构化历史目录替换的边界回归。"""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from agent_core.core.history_context import HistoryContext
from agent_core.core.storage import SessionStore


def _link_directory(link, target):
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_path_reader_uses_requested_file_without_directory_fd(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text('{"synthetic": true}', encoding='utf-8')
    assert HistoryContext._read_json(path) == {'synthetic': True}


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction 的临时正文写入窗口')
def test_publish_rejects_junction_before_any_private_temporary_bytes(tmp_path, monkeypatch):
    from agent_core.core import files

    session = SimpleNamespace(id='a' * 32)
    store = SessionStore(tmp_path / 'state')
    try:
        folder = store.directory(session.id) / 'context'
        folder.mkdir(parents=True)
        outside = tmp_path / 'outside'
        outside.mkdir()
        _link_directory(folder / 'chunks', outside)
        service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
        observed = []
        original = files.atomic_replace

        def observe(source, target, **kwargs):
            observed.extend(b'private-canary' in path.read_bytes() for path in outside.iterdir() if path.is_file())
            return original(source, target, **kwargs)

        monkeypatch.setattr(files, 'atomic_replace', observe)
        with pytest.raises((PermissionError, OSError)):
            service._publish(session, [{'ID': 'b' * 32, 'RawHistory': 'private-canary'}], {'revision': 1})
        assert not any(observed), 'private bytes were written outside before rejection'
        assert list(outside.iterdir()) == []
    finally:
        store.close()


@pytest.mark.parametrize('component', ['context', 'chunks'])
def test_linked_history_directory_rejected_before_read_or_write(tmp_path, component):
    session = SimpleNamespace(id='a' * 32)
    store = SessionStore(tmp_path / 'state')
    try:
        session_dir = store.directory(session.id)
        session_dir.mkdir()
        outside = tmp_path / 'outside'
        outside.mkdir()
        folder = session_dir / 'context'
        if component == 'context':
            _link_directory(folder, outside)
        else:
            folder.mkdir()
            _link_directory(folder / 'chunks', outside)
        service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
        with pytest.raises((PermissionError, OSError)):
            service._load(session)
        record = {'ID': 'b' * 32, 'RawHistory': 'private-canary'}
        with pytest.raises((PermissionError, OSError)):
            service._publish(session, [record], {'revision': 1})
        assert list(outside.iterdir()) == []
    finally:
        store.close()


@pytest.mark.skipif(os.name == "nt", reason="此测试使用 POSIX 符号链接竞态")
def test_manifest_directory_replaced_after_check(tmp_path, monkeypatch):
    root = tmp_path / "state"
    session = SimpleNamespace(id="a" * 32)
    store = SessionStore(root)
    try:
        folder = store.directory(session.id) / "context"
        folder.mkdir(parents=True)
        (folder / "chunks").mkdir()
        (folder / "manifest.json").write_text(json.dumps({
            "version": 1, "owner_id": session.id, "revision": 1,
            "session_summary": "", "chunks": [], "summary_kind": "none", "updated_at": "",
        }))
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "manifest.json").write_text(json.dumps({
            "version": 1, "owner_id": session.id, "revision": 0,
            "session_summary": "", "chunks": [], "summary_kind": "none", "updated_at": "",
        }))
        service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
        original = service._read_json

        def swap(path, directory_fd):
            folder.rename(folder.with_name("original"))
            folder.symlink_to(outside, target_is_directory=True)
            return original(path, directory_fd)

        monkeypatch.setattr(service, "_read_json", swap)
        assert service._load(session)["revision"] == 1
    finally:
        store.close()


@pytest.mark.skipif(os.name == 'nt', reason='Windows 目录句柄阻止重命名')
def test_chunk_directory_swap_does_not_redirect_write(tmp_path, monkeypatch):
    session = SimpleNamespace(id='a' * 32)
    store = SessionStore(tmp_path / 'state')
    try:
        folder = store.directory(session.id) / 'context'
        chunks = folder / 'chunks'
        chunks.mkdir(parents=True)
        outside = tmp_path / 'outside'
        outside.mkdir()
        service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
        original = service._write_history

        def swap(path, directory_fd, text, *, immutable=False):
            if immutable:
                chunks.rename(folder / 'original')
                chunks.symlink_to(outside, target_is_directory=True)
            return original(path, directory_fd, text, immutable=immutable)

        monkeypatch.setattr(service, '_write_history', swap)
        service._publish(session, [{'ID': 'b' * 32, 'RawHistory': 'private-canary'}], {'revision': 1})
        assert list(outside.iterdir()) == []
        assert (folder / 'original' / ('b' * 32 + '.json')).exists()
    finally:
        store.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX 目录相对写入')
def test_failed_write_cleans_temporary_file(tmp_path, monkeypatch):
    session = SimpleNamespace(id='a' * 32)
    store = SessionStore(tmp_path / 'state')
    try:
        folder = store.directory(session.id) / 'context'
        (folder / 'chunks').mkdir(parents=True)
        service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
        with service._directories(session) as (_, _, _, chunks_fd):
            def fail_sync(_):
                raise OSError('injected failure')
            monkeypatch.setattr(os, 'fsync', fail_sync)
            with pytest.raises(OSError):
                service._write_history(folder / 'chunks' / 'item.json', chunks_fd,
                                       'private-canary', immutable=True)
        assert list((folder / 'chunks').iterdir()) == []
    finally:
        store.close()


def test_hardlinked_manifest_is_rejected(tmp_path):
    session = SimpleNamespace(id='a' * 32)
    store = SessionStore(tmp_path / 'state')
    try:
        folder = store.directory(session.id) / 'context'
        (folder / 'chunks').mkdir(parents=True)
        foreign = tmp_path / 'foreign.json'
        foreign.write_text('{}')
        os.link(foreign, folder / 'manifest.json')
        service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
        with pytest.raises((PermissionError, ValueError)):
            service._load(session)
    finally:
        store.close()


def test_replaced_published_chunk_does_not_commit_manifest(tmp_path, monkeypatch):
    session = SimpleNamespace(id='a' * 32)
    store = SessionStore(tmp_path / 'state')
    try:
        folder = store.directory(session.id) / 'context'
        (folder / 'chunks').mkdir(parents=True)
        manifest_path = folder / 'manifest.json'
        manifest_path.write_text('{"revision":0}', encoding='utf-8')
        old_manifest = manifest_path.read_bytes()
        service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
        original = service._write_history

        def replace_chunk(path, directory_fd, text, *, immutable=False):
            original(path, directory_fd, text, immutable=immutable)
            if immutable:
                replacement = path.parent / 'replacement.json'
                replacement.write_text('{"RawHistory":"different"}', encoding='utf-8')
                os.replace(replacement, path)

        monkeypatch.setattr(service, '_write_history', replace_chunk)
        with pytest.raises(ValueError, match='历史块发布后内容不匹配'):
            service._publish(session, [{'ID': 'b' * 32, 'RawHistory': 'private-canary'}],
                             {'revision': 1})
        assert manifest_path.read_bytes() == old_manifest
    finally:
        store.close()


def test_replaced_staging_chunk_does_not_commit_manifest(tmp_path, monkeypatch):
    session = SimpleNamespace(id='a' * 32)
    store = SessionStore(tmp_path / 'state')
    try:
        folder = store.directory(session.id) / 'context'
        chunks = folder / 'chunks'
        chunks.mkdir(parents=True)
        manifest_path = folder / 'manifest.json'
        manifest_path.write_text('{"revision":0}', encoding='utf-8')
        old_manifest = manifest_path.read_bytes()
        service = HistoryContext(SimpleNamespace(store=store), supervisor=object())
        original_link = os.link

        def replace_staging(source, target, **kwargs):
            replacement = chunks / 'replacement.json'
            replacement.write_text('{"RawHistory":"different"}', encoding='utf-8')
            staging = chunks / source if isinstance(source, str) else source
            os.replace(replacement, staging)
            return original_link(source, target, **kwargs)

        monkeypatch.setattr(os, 'link', replace_staging)
        with pytest.raises(ValueError, match='历史块发布后内容不匹配'):
            service._publish(session, [{'ID': 'b' * 32, 'RawHistory': 'private-canary'}],
                             {'revision': 1})
        assert manifest_path.read_bytes() == old_manifest
    finally:
        store.close()
