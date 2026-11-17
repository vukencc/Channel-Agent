"""Windows 原子文本写入的句柄和失败边界。"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要真实 Windows 句柄")


def test_writer_creates_source_once_and_preserves_unicode(tmp_path: Path, monkeypatch) -> None:
    from agent_core.core import windows_files
    from agent_core.core.storage import SessionStore

    calls = []
    original = windows_files._create

    def record(path, access, share, security, disposition, flags, template):
        if Path(path).name.startswith(".writing-"):
            calls.append((access, share, disposition))
        return original(path, access, share, security, disposition, flags, template)

    monkeypatch.setattr(windows_files, "_create", record)
    target = tmp_path / "状态.json"
    SessionStore.atomic_write(target, "中文\nsecond\n")
    assert target.read_bytes() == "中文\r\nsecond\r\n".encode("utf-8")
    assert len(calls) == 1
    access, share, disposition = calls[0]
    assert access & 0xC0010000 == 0xC0010000
    assert share & 4 == 0
    assert disposition == 1
    assert not list(tmp_path.glob(".writing-*"))


@pytest.mark.parametrize("failure", ["fsync", "rename"])
def test_writer_failure_cleans_source_and_preserves_target(tmp_path: Path, monkeypatch, failure: str) -> None:
    from agent_core.core import windows_files
    from agent_core.core.storage import SessionStore

    target = tmp_path / "state"
    target.write_text("old", encoding="utf-8")
    def fail(*args, **kwargs):
        raise OSError("injected failure")
    if failure == "fsync":
        monkeypatch.setattr(windows_files.os, "fsync", fail)
    else:
        monkeypatch.setattr(windows_files, "_verified_replace_handle", fail)
    with pytest.raises(OSError):
        SessionStore.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".writing-*"))


def test_writer_rejects_hardlinked_target(tmp_path: Path) -> None:
    from agent_core.core.storage import SessionStore

    target = tmp_path / "state"
    target.write_text("old", encoding="utf-8")
    os.link(target, tmp_path / "link")
    with pytest.raises(PermissionError):
        SessionStore.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".writing-*"))


def test_writer_rejects_open_target_and_cleans_up(tmp_path: Path) -> None:
    from agent_core.core.storage import SessionStore
    from agent_core.core.windows_files import opened_path

    target = tmp_path / "state"
    target.write_text("old", encoding="utf-8")
    with opened_path(tmp_path, target):
        with pytest.raises(OSError):
            SessionStore.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".writing-*"))


def test_writer_encoding_failure_cleans_up(tmp_path: Path) -> None:
    from agent_core.core.storage import SessionStore

    target = tmp_path / "state"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        SessionStore.atomic_write(target, "\ud800")
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".writing-*"))


def test_post_rename_flush_failure_keeps_published_target(tmp_path: Path, monkeypatch) -> None:
    import ctypes
    from agent_core.core import windows_files
    from agent_core.core.storage import SessionStore

    target = tmp_path / "state"
    target.write_text("old", encoding="utf-8")
    original = windows_files._kernel.FlushFileBuffers
    count = 0

    def flush(handle):
        nonlocal count
        count += 1
        if count == 2:
            ctypes.set_last_error(5)
            return 0
        return original(handle)

    with monkeypatch.context() as patch:
        patch.setattr(windows_files._kernel, "FlushFileBuffers", flush)
        with pytest.raises(OSError):
            SessionStore.atomic_write(target, "new")
    assert count == 2
    assert target.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".writing-*"))
    SessionStore.atomic_write(target, "later")
    assert target.read_text(encoding="utf-8") == "later"


def test_writer_collision_preserves_existing_temporary(tmp_path: Path, monkeypatch) -> None:
    from agent_core.core import windows_files
    from agent_core.core.storage import SessionStore

    target = tmp_path / "state"
    target.write_text("old", encoding="utf-8")
    temporary = tmp_path / ".writing-fixed"
    temporary.write_text("other", encoding="utf-8")
    monkeypatch.setattr(windows_files.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    with pytest.raises(FileExistsError):
        SessionStore.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert temporary.read_text(encoding="utf-8") == "other"
