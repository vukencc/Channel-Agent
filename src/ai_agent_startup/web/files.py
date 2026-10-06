"""只读文件预览：通过目录句柄逐层打开，拒绝验证后的符号链接替换。"""
from contextlib import contextmanager
import errno
import itertools
import os
from pathlib import Path
import stat


@contextmanager
def opened_path(root: Path, target: Path, *, directory=False):
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        raise PermissionError('当前平台不支持安全目录句柄，文件预览不可用')
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise PermissionError('路径不属于当前工作区') from None
    descriptor = None
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for index, part in enumerate(parts):
            if part in {'', '.', '..'}:
                raise PermissionError('路径分量无效')
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory or index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise ValueError('只支持普通文本文件或目录预览')
        yield descriptor
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            raise PermissionError('路径权限或类型已发生变化，拒绝预览') from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def directory_entries(root: Path, target: Path):
    entries = []
    with opened_path(root, target, directory=True) as descriptor:
        with os.scandir(descriptor) as scan:
            for item in itertools.islice(scan, 500):
                if item.is_symlink():
                    continue
                try:
                    info = item.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                entries.append({'name': item.name, 'path': str((target / item.name).relative_to(root)),
                                'is_dir': stat.S_ISDIR(info.st_mode), 'size': info.st_size})
    return sorted(entries, key=lambda item: (not item['is_dir'], item['name'].casefold()))


def text_preview(root: Path, target: Path):
    with opened_path(root, target) as descriptor:
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            data = stream.read(262145)
    if b'\x00' in data:
        raise ValueError('此文件不是可预览文本')
    return {'content': data[:262144].decode('utf-8', errors='replace'), 'truncated': len(data) > 262144}
