"""通过目录句柄安全打开工作区文件，供运行时和界面共同使用。"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat


@contextmanager
def opened_path(root: Path, target: Path, *, directory=False, expected_identity=None):
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        raise PermissionError('当前平台不支持安全目录句柄，文件预览不可用')
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise PermissionError('路径不属于当前工作区') from None
    descriptor = None
    try:
        if not root.is_absolute() or '..' in root.parts:
            raise PermissionError('工作区必须为 Linux 绝对目录')
        descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in root.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        info = os.fstat(descriptor)
        if expected_identity is not None and (info.st_dev, info.st_ino) != tuple(expected_identity):
            raise PermissionError('工作区目录身份已变化，拒绝预览')
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
