# 工作区共享记忆的有界、原子快照。
# 锁只协调遵守协议的框架写者；同权限外部进程可修改项目资料或锁。
# update_id 在最近 24 条进度记录内幂等，不构成永久去重集合。
from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .files import atomic_replace, fsync_directory, opened_path

DIR_NAME = ".channelagent"
FILE_NAME = "memory.json"
MAX_BYTES = 128 * 1024


class WorkspaceMemoryCommitUncertain(RuntimeError):
    """发布可能成功，但持久化或完成审计未确认。"""


def _clean(value: str) -> str:
    value = value.strip()
    if not value or "\x00" in value:
        raise ValueError("文本不能为空或包含 NUL")
    value.encode("utf-8", "strict")
    return value


class WorkspaceMemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: list[str] = Field(default_factory=list, max_length=8)
    progress: str = Field(max_length=1200)
    lessons: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("project", "lessons")
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        result = [_clean(v) for v in values]
        if any(len(v) > 500 for v in result):
            raise ValueError("文本超过长度限制")
        return result

    @field_validator("progress")
    @classmethod
    def clean_progress(cls, value: str) -> str:
        return _clean(value)


class ProgressRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    update_id: str = Field(min_length=1, max_length=200)
    time: datetime
    outcome: Literal["idle", "checkpoint", "error"]
    summary: str = Field(min_length=1, max_length=1200)

    @field_validator("update_id", "summary")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return _clean(value)

    @field_validator("time")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("时间必须为 UTC")
        return value


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    revision: int = Field(ge=1)
    updated_at: datetime
    project: list[str] = Field(max_length=48)
    progress: list[ProgressRecord] = Field(max_length=24)
    lessons: list[str] = Field(max_length=48)

    @field_validator("project", "lessons")
    @classmethod
    def validate_facts(cls, values: list[str]) -> list[str]:
        values = [_clean(v) for v in values]
        if any(len(v) > 500 for v in values):
            raise ValueError("文本超过长度限制")
        return values

    @field_validator("updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("时间必须为 UTC")
        return value


def _merged_snapshot(old: Snapshot | None, delta: WorkspaceMemoryDelta,
                     record: ProgressRecord) -> tuple[Snapshot, bytes]:
    def merge(existing: list[str], incoming: list[str], limit: int) -> list[str]:
        return list(dict.fromkeys(existing + incoming))[-limit:]

    snapshot = Snapshot(revision=old.revision + 1 if old else 1,
                        updated_at=datetime.now(timezone.utc),
                        project=merge(old.project if old else [], delta.project, 48),
                        progress=((old.progress if old else []) + [record])[-24:],
                        lessons=merge(old.lessons if old else [], delta.lessons, 48))
    sections = [snapshot.project, snapshot.progress, snapshot.lessons]
    while True:
        payload = snapshot.model_dump_json().encode("utf-8")
        if len(payload) <= MAX_BYTES:
            return snapshot, payload
        # 每轮裁剪当前字节占用最大的区段，保留每区最新一条。
        eligible = [section for section in sections if len(section) > 1]
        if not eligible:
            raise ValueError("记忆快照超过大小限制")
        largest = max(eligible, key=lambda section: sum(
            len((item.model_dump_json() if isinstance(item, BaseModel) else item).encode("utf-8"))
            for item in section))
        del largest[0]


class WorkspaceMemoryStore:
    def __init__(self, root: Path, expected_identity: tuple[int, int]):
        self.root = Path(root)
        self.expected_identity = expected_identity
        self.directory = self.root / DIR_NAME
        self.path = self.directory / FILE_NAME

    def read(self) -> Snapshot | None:
        try:
            with opened_path(self.root, self.path, expected_identity=self.expected_identity, allow_replace=True) as fd:
                if os.fstat(fd).st_size > MAX_BYTES:
                    raise ValueError("记忆快照超过大小限制")
                data = b""
                while len(data) <= MAX_BYTES:
                    chunk = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
                if len(data) > MAX_BYTES:
                    raise ValueError("记忆快照超过大小限制")
                return Snapshot.model_validate_json(data)
        except FileNotFoundError:
            # 即使文件不存在也验证根目录身份。
            with opened_path(self.root, self.root, directory=True, expected_identity=self.expected_identity):
                pass
            return None

    def append(self, delta: WorkspaceMemoryDelta, *, update_id: str, outcome: str,
               cancelled: threading.Event, authorize: Callable[[], bool],
               audit: Callable[..., None]) -> Snapshot | None:
        record = ProgressRecord(update_id=update_id, time=datetime.now(timezone.utc),
                                outcome=outcome, summary=delta.progress)
        if cancelled.is_set() or not authorize():
            return None
        self.read()
        audit("workspace_memory_started")
        if cancelled.is_set():
            return None
        if os.name == "nt":
            return self._append_windows(delta, record, cancelled, audit)
        with opened_path(self.root, self.root, directory=True, expected_identity=self.expected_identity) as root_fd:
            try:
                os.mkdir(DIR_NAME, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            # 即使目录已存在，也同步根目录，涵盖此前创建后同步失败的重试。
            os.fsync(root_fd)
            with opened_path(self.root, self.directory, directory=True, expected_identity=self.expected_identity) as directory_fd:
                deadline = time.monotonic() + 3
                while True:
                    if cancelled.is_set():
                        return None
                    try:
                        os.mkdir(".write-lock", 0o700, dir_fd=directory_fd)
                        break
                    except FileExistsError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("工作区记忆写锁超时")
                        cancelled.wait(0.05)
                lock_fd = None
                published = False
                try:
                    lock_fd = os.open(".write-lock", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    lock_stat = os.fstat(lock_fd)
                    with opened_path(self.root, self.directory, directory=True, expected_identity=self.expected_identity) as current_fd:
                        if (os.fstat(current_fd).st_dev, os.fstat(current_fd).st_ino) != (os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino):
                            raise PermissionError("记忆目录身份已变化")
                    old = self.read()
                    if old and any(p.update_id == record.update_id for p in old.progress):
                        audit("workspace_memory_updated", revision=old.revision, unchanged=True)
                        return old
                    if cancelled.is_set():
                        return None
                    snapshot, payload = _merged_snapshot(old, delta, record)
                    temp = ".memory-" + secrets.token_hex(16)
                    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
                    try:
                        with os.fdopen(fd, "wb") as file:
                            file.write(payload)
                            file.flush()
                            os.fsync(file.fileno())
                        if cancelled.is_set():
                            return None
                        # 替换前再次拒绝被替换的目标。
                        self.read()
                        os.replace(temp, FILE_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                        published = True
                        os.fsync(directory_fd)
                        audit("workspace_memory_updated", revision=snapshot.revision)
                    except BaseException as exc:
                        if published:
                            raise WorkspaceMemoryCommitUncertain("记忆可能已发布") from exc
                        raise
                    finally:
                        if not published:
                            try:
                                os.unlink(temp, dir_fd=directory_fd)
                            except FileNotFoundError:
                                pass
                    return snapshot
                finally:
                    if lock_fd is not None:
                        try:
                            try:
                                current = os.stat(".write-lock", dir_fd=directory_fd, follow_symlinks=False)
                                if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (lock_stat.st_dev, lock_stat.st_ino):
                                    raise PermissionError("记忆写锁身份已变化")
                                os.rmdir(".write-lock", dir_fd=directory_fd)
                            finally:
                                os.close(lock_fd)
                        except BaseException as exc:
                            if published:
                                raise WorkspaceMemoryCommitUncertain("记忆可能已发布，锁释放失败") from exc
                            raise

    def _append_windows(self, delta: WorkspaceMemoryDelta, record: ProgressRecord,
                        cancelled: threading.Event, audit: Callable[..., None]) -> Snapshot | None:
        # Windows 句柄阻止祖先目录重命名或删除，直到整个操作结束。
        from .windows_files import directory_identity

        lock = self.directory / ".write-lock"
        with opened_path(self.root, self.root, directory=True, expected_identity=self.expected_identity):
            try:
                os.mkdir(self.directory)
            except FileExistsError:
                pass
            with opened_path(self.root, self.directory, directory=True, expected_identity=self.expected_identity):
                deadline = time.monotonic() + 3
                while True:
                    if cancelled.is_set():
                        return None
                    try:
                        os.mkdir(lock)
                        break
                    except FileExistsError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("工作区记忆写锁超时")
                        cancelled.wait(0.05)
                lock_identity = directory_identity(lock)
                published = False
                try:
                    with opened_path(self.root, lock, directory=True, expected_identity=self.expected_identity):
                        old = self.read()
                        if old and any(p.update_id == record.update_id for p in old.progress):
                            audit("workspace_memory_updated", revision=old.revision, unchanged=True)
                            return old
                        if cancelled.is_set():
                            return None
                        snapshot, payload = _merged_snapshot(old, delta, record)
                        temp = self.directory / (".memory-" + secrets.token_hex(16))
                        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
                        try:
                            with os.fdopen(fd, "wb") as file:
                                file.write(payload)
                                file.flush()
                                os.fsync(file.fileno())
                            if cancelled.is_set():
                                return None
                            self.read()
                            # Windows 替换操作可能在异常后已经生效。
                            try:
                                atomic_replace(temp, self.path, replace_open=True)
                                published = True
                                fsync_directory(self.directory)
                                audit("workspace_memory_updated", revision=snapshot.revision)
                            except BaseException as exc:
                                raise WorkspaceMemoryCommitUncertain("记忆可能已发布") from exc
                        finally:
                            if not published:
                                try:
                                    os.unlink(temp)
                                except FileNotFoundError:
                                    pass
                        return snapshot
                except BaseException as exc:
                    if published and not isinstance(exc, WorkspaceMemoryCommitUncertain):
                        raise WorkspaceMemoryCommitUncertain("记忆可能已发布") from exc
                    raise
                finally:
                    try:
                        if directory_identity(lock) != lock_identity:
                            raise PermissionError("记忆写锁身份已变化")
                        os.rmdir(lock)
                    except BaseException as exc:
                        if published:
                            raise WorkspaceMemoryCommitUncertain("记忆可能已发布，锁释放失败") from exc
                        raise


def render(snapshot: Snapshot, max_chars: int = 6000) -> str:
    if max_chars <= 0:
        return ""
    header = "以下为项目参考资料及自报进度，未经业务验收。\n"
    sections = ["项目事实", "近期进度", "经验教训"]
    values = [list(reversed(snapshot.project)), [f"{p.time.isoformat()} {p.outcome}: {p.summary}" for p in reversed(snapshot.progress)], list(reversed(snapshot.lessons))]
    remaining = max(0, max_chars - len(header))
    budgets = [remaining // 3] * 3
    budgets[-1] += remaining - sum(budgets)
    return (header + "".join((f"{title}:\n" + "\n".join(items) + "\n")[:budget]
                             for title, items, budget in zip(sections, values, budgets)))[:max_chars]
