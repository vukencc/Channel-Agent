import threading
from pathlib import Path

from agent_core.core.files import directory_identity
from agent_core.core.workspace_memory import WorkspaceMemoryDelta, WorkspaceMemoryStore


def _process_append(args):
    root, identity, index = args
    store = WorkspaceMemoryStore(Path(root), identity)
    return store.append(WorkspaceMemoryDelta(project=[f"fact-{index}"], progress=f"progress-{index}"),
                        update_id=f"update-{index}", outcome="idle", cancelled=threading.Event(),
                        authorize=lambda: True, audit=lambda *args, **kwargs: None).revision


def test_spawned_processes_preserve_all_updates(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    identity = directory_identity(tmp_path)
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as executor:
        futures = [executor.submit(_process_append, (str(tmp_path), identity, index)) for index in range(12)]
        for future in futures:
            future.result(timeout=30)
    snapshot = WorkspaceMemoryStore(tmp_path, identity).read()
    assert snapshot.revision == 12
    assert set(snapshot.project) == {f"fact-{index}" for index in range(12)}
    assert {item.update_id for item in snapshot.progress} == {f"update-{index}" for index in range(12)}


def test_new_directory_parent_sync_failure_prevents_commit(tmp_path, monkeypatch):
    import os
    import pytest
    import agent_core.core.workspace_memory as memory

    if os.name == "nt":
        pytest.skip("Windows 没有 POSIX 目录 fsync")
    identity = directory_identity(tmp_path)
    store = WorkspaceMemoryStore(tmp_path, identity)
    original = memory.os.fsync
    def fail_root(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            raise OSError("root sync failed")
        original(fd)
    monkeypatch.setattr(memory.os, "fsync", fail_root)
    events = []
    with pytest.raises(OSError, match="root sync failed"):
        store.append(WorkspaceMemoryDelta(progress="done"), update_id="x", outcome="idle",
                     cancelled=threading.Event(), authorize=lambda: True,
                     audit=lambda event, **kwargs: events.append(event))
    assert store.read() is None
    assert "workspace_memory_updated" not in events


def test_read_is_side_effect_free_and_append_recovers(tmp_path):
    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    assert store.read() is None
    assert list(tmp_path.iterdir()) == []
    delta = WorkspaceMemoryDelta(project=[" fact "], progress="done", lessons=["lesson"])
    result = store.append(delta, update_id="one", outcome="idle", cancelled=threading.Event(),
                          authorize=lambda: True, audit=lambda *args, **kwargs: None)
    assert result.revision == 1
    assert store.read() == result
    assert result.project == ["fact"]


def test_concurrent_updates_and_idempotency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    identity = directory_identity(tmp_path)
    def write(index):
        return WorkspaceMemoryStore(tmp_path, identity).append(
            WorkspaceMemoryDelta(project=[str(index)], progress=str(index)),
            update_id=str(index), outcome="idle", cancelled=threading.Event(),
            authorize=lambda: True, audit=lambda *args, **kwargs: None)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(8)))
    result = WorkspaceMemoryStore(tmp_path, identity).read()
    assert result.revision == 8
    assert set(result.project) == set(map(str, range(8)))
    assert write(0).revision == 8


def test_denial_and_corrupt_snapshot(tmp_path):
    import pytest

    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    delta = WorkspaceMemoryDelta(progress="done")
    assert store.append(delta, update_id="x", outcome="idle", cancelled=threading.Event(),
                        authorize=lambda: False, audit=lambda *args, **kwargs: None) is None
    assert list(tmp_path.iterdir()) == []
    directory = tmp_path / ".channelagent"
    directory.mkdir()
    path = directory / "memory.json"
    path.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        store.append(delta, update_id="x", outcome="idle", cancelled=threading.Event(),
                     authorize=lambda: True, audit=lambda *args, **kwargs: None)
    assert path.read_bytes() == b"invalid"


def test_completed_audit_failure_is_uncertain(tmp_path):
    import pytest
    from agent_core.core.workspace_memory import WorkspaceMemoryCommitUncertain

    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    def audit(event, **kwargs):
        if event == "workspace_memory_updated":
            raise RuntimeError("audit failed")
    with pytest.raises(WorkspaceMemoryCommitUncertain):
        store.append(WorkspaceMemoryDelta(progress="done"), update_id="x", outcome="idle",
                     cancelled=threading.Event(), authorize=lambda: True, audit=audit)
    assert store.read().revision == 1


def test_links_and_root_identity_are_rejected(tmp_path):
    import os
    import pytest

    root = tmp_path / "root"
    root.mkdir()
    store = WorkspaceMemoryStore(root, directory_identity(root))
    directory = root / ".channelagent"
    directory.mkdir()
    target = tmp_path / "target"
    target.write_text("{}")
    path = directory / "memory.json"
    try:
        path.symlink_to(target)
    except OSError:
        pass  # Windows 可能没有创建符号链接的权限。
    else:
        with pytest.raises((PermissionError, OSError)):
            store.read()
        path.unlink()
    os.link(target, path)
    with pytest.raises(PermissionError):
        store.read()
    path.unlink()
    root.rename(tmp_path / "old")
    root.mkdir()
    with pytest.raises(PermissionError):
        store.read()


def test_cancel_and_start_audit_failure_do_not_write(tmp_path):
    import pytest

    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    cancelled = threading.Event()
    cancelled.set()
    args = dict(update_id="x", outcome="idle", cancelled=cancelled,
                authorize=lambda: True, audit=lambda *args, **kwargs: None)
    assert store.append(WorkspaceMemoryDelta(progress="done"), **args) is None
    assert list(tmp_path.iterdir()) == []
    cancelled.clear()
    def fail(*args, **kwargs):
        raise RuntimeError("audit")
    args["audit"] = fail
    with pytest.raises(RuntimeError):
        store.append(WorkspaceMemoryDelta(progress="done"), **args)
    assert list(tmp_path.iterdir()) == []


def test_cancel_during_started_audit_creates_nothing(tmp_path):
    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    cancelled = threading.Event()
    def audit(event, **metadata):
        if event == "workspace_memory_started":
            cancelled.set()
    result = store.append(WorkspaceMemoryDelta(progress="done"), update_id="x", outcome="idle",
                          cancelled=cancelled, authorize=lambda: True, audit=audit)
    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_failed_replace_preserves_previous_snapshot(tmp_path, monkeypatch):
    import os
    import pytest
    import agent_core.core.workspace_memory as memory

    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    def append(value):
        return store.append(WorkspaceMemoryDelta(progress=value), update_id=value, outcome="idle",
                            cancelled=threading.Event(), authorize=lambda: True,
                            audit=lambda *args, **kwargs: None)
    first = append("first")
    def fail(*args, **kwargs):
        raise OSError("injected")
    if os.name == "nt":
        monkeypatch.setattr(memory, "atomic_replace", fail)
        expected = memory.WorkspaceMemoryCommitUncertain
    else:
        monkeypatch.setattr(memory.os, "replace", fail)
        expected = OSError
    with pytest.raises(expected):
        append("second")
    assert store.read() == first


def test_locked_directory_times_out(tmp_path, monkeypatch):
    import pytest
    import agent_core.core.workspace_memory as memory

    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    (tmp_path / ".channelagent" / ".write-lock").mkdir(parents=True)
    values = iter([0, 4])
    monkeypatch.setattr(memory.time, "monotonic", lambda: next(values))
    with pytest.raises(TimeoutError):
        store.append(WorkspaceMemoryDelta(progress="done"), update_id="x", outcome="idle",
                     cancelled=threading.Event(), authorize=lambda: True,
                     audit=lambda *args, **kwargs: None)
    assert (tmp_path / ".channelagent" / ".write-lock").is_dir()


def test_render_prefers_recent_progress_and_lessons(tmp_path):
    from agent_core.core.workspace_memory import render
    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    for index in range(8):
        result = store.append(WorkspaceMemoryDelta(progress=("旧" * 400 if index < 7 else "最新进度"),
                                                   lessons=["旧" * 400 if index < 7 else "最新经验"]),
                              update_id=str(index), outcome="idle", cancelled=threading.Event(),
                              authorize=lambda: True, audit=lambda *args, **kwargs: None)
    text = render(result, max_chars=300)
    assert "最新进度" in text
    assert "最新经验" in text


def test_idempotent_update_has_completion_audit(tmp_path):
    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    events = []
    def append():
        return store.append(WorkspaceMemoryDelta(progress="done"), update_id="same", outcome="idle",
                            cancelled=threading.Event(), authorize=lambda: True,
                            audit=lambda event, **metadata: events.append((event, metadata)))
    append()
    events.clear()
    append()
    assert [event for event, _ in events] == ["workspace_memory_started", "workspace_memory_updated"]
    assert events[-1][1]["revision"] == 1


def test_large_valid_content_remains_writable(tmp_path):
    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    for index in range(12):
        fact = str(index) + "中" * 490
        delta = WorkspaceMemoryDelta(project=[fact + str(i) for i in range(8)],
                                     lessons=[fact + str(i) for i in range(8)],
                                     progress=str(index) + "中" * 1100)
        result = store.append(delta, update_id=str(index), outcome="idle", cancelled=threading.Event(),
                              authorize=lambda: True, audit=lambda *args, **kwargs: None)
    assert result.revision == 12
    assert result.progress[-1].summary.startswith("11")
    assert result.project[-1].startswith("11")
    assert result.lessons[-1].startswith("11")
    assert store.path.stat().st_size <= 128 * 1024


def test_lock_release_failure_after_publish_is_uncertain(tmp_path, monkeypatch):
    import pytest
    import agent_core.core.workspace_memory as memory

    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    def fail(*args, **kwargs):
        raise OSError("lock release failed")
    monkeypatch.setattr(memory.os, "rmdir", fail)
    with pytest.raises(memory.WorkspaceMemoryCommitUncertain):
        store.append(WorkspaceMemoryDelta(progress="done"), update_id="x", outcome="idle",
                     cancelled=threading.Event(), authorize=lambda: True,
                     audit=lambda *args, **kwargs: None)
    assert store.read().revision == 1


def test_idempotent_completion_audit_failure_propagates(tmp_path):
    import pytest
    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    args = dict(update_id="x", outcome="idle", cancelled=threading.Event(), authorize=lambda: True)
    delta = WorkspaceMemoryDelta(progress="done")
    store.append(delta, **args, audit=lambda *args, **kwargs: None)
    def audit(event, **metadata):
        if event == "workspace_memory_updated":
            raise RuntimeError("audit failed")
    with pytest.raises(RuntimeError, match="audit failed"):
        store.append(delta, **args, audit=audit)
    assert store.read().revision == 1


def test_directory_sync_failure_after_publish_is_uncertain(tmp_path, monkeypatch):
    import os
    import stat
    import pytest
    import agent_core.core.workspace_memory as memory

    store = WorkspaceMemoryStore(tmp_path, directory_identity(tmp_path))
    def fail(*args):
        raise OSError("sync")
    if os.name == "nt":
        monkeypatch.setattr(memory, "fsync_directory", fail)
    else:
        original = memory.os.fsync
        def fail_directory(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) != store.expected_identity:
                fail()
            original(fd)
        monkeypatch.setattr(memory.os, "fsync", fail_directory)
    with pytest.raises(memory.WorkspaceMemoryCommitUncertain):
        store.append(WorkspaceMemoryDelta(progress="done"), update_id="x", outcome="idle",
                     cancelled=threading.Event(), authorize=lambda: True,
                     audit=lambda *args, **kwargs: None)
    assert store.read().revision == 1
