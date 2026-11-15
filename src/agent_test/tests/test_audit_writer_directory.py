"""相对安全目录句柄追加审计，不能经路径替换写到其他目录。"""
import os
from pathlib import Path

import pytest

from agent_core.core.audit_writer import append_audit, close_audit_handles


pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX dir_fd 接口；Windows 使用固定目录链')


@pytest.fixture(autouse=True)
def audit_handles():
    close_audit_handles()
    yield
    close_audit_handles()


def test_directory_handle_survives_path_replacement_without_following_link(tmp_path: Path):
    private, outside = tmp_path / 'private', tmp_path / 'outside'
    private.mkdir()
    outside.mkdir()
    descriptor = os.open(private, os.O_RDONLY | os.O_DIRECTORY)
    moved = tmp_path / 'original-directory'
    try:
        private.rename(moved)
        private.symlink_to(outside, target_is_directory=True)
        append_audit(private / 'audit.jsonl', 'synthetic-event', sync=True, directory_fd=descriptor)
        assert (moved / 'audit.jsonl').read_text() == 'synthetic-event\n'
        assert list(outside.iterdir()) == []
    finally:
        os.close(descriptor)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo'])
def test_directory_audit_rejects_unsafe_leaf_before_write(tmp_path: Path, kind: str):
    private = tmp_path / 'private'
    private.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'unchanged')
    target = private / 'audit.jsonl'
    if kind == 'symlink':
        target.symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, target)
    else:
        os.mkfifo(target)
    descriptor = os.open(private, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((PermissionError, OSError)):
            append_audit(target, 'must-not-be-written', directory_fd=descriptor)
        assert outside.read_bytes() == b'unchanged'
    finally:
        os.close(descriptor)


def test_directory_audit_refreshes_cached_file_from_authorized_directory(tmp_path: Path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    one = os.open(first, os.O_RDONLY | os.O_DIRECTORY)
    two = os.open(second, os.O_RDONLY | os.O_DIRECTORY)
    path = first / 'audit.jsonl'
    try:
        append_audit(path, 'first', directory_fd=one)
        append_audit(path, 'second', directory_fd=two)
        assert path.read_text() == 'first\n'
        assert (second / 'audit.jsonl').read_text() == 'second\n'
    finally:
        os.close(one)
        os.close(two)
