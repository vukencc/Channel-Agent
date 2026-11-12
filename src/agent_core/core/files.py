"""通过目录句柄安全打开工作区文件，供运行时和界面共同使用。"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat


@contextmanager
def opened_path(root: Path, target: Path, *, directory=False, expected_identity=None,
                writable: bool = False, allow_replace: bool = False):
    if allow_replace and (directory or writable):
        raise ValueError('原子快照替换只适用于只读普通文件')
    if os.name == 'nt':
        from .windows_files import opened_path as windows_opened_path
        options = {'allow_replace': True} if allow_replace else {}
        with windows_opened_path(root, target, directory=directory, expected_identity=expected_identity,
                                 writable=writable, **options) as descriptor:
            yield descriptor
        return
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
            elif writable:
                flags |= os.O_RDWR
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        info = os.fstat(descriptor)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ValueError('只支持普通文本文件或目录预览')
        if not directory and info.st_nlink != 1:
            raise PermissionError('无法确认硬链接全部位于授权目录，拒绝打开文件')
        yield descriptor
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            raise PermissionError('路径权限或类型已发生变化，拒绝预览') from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def directory_identity(path: Path) -> tuple[int, int]:
    """取得目录身份，供工作区变更检测使用。"""
    if os.name == 'nt':
        from .windows_files import directory_identity as windows_directory_identity
        return windows_directory_identity(path)
    with opened_path(path, path, directory=True) as descriptor:
        info = os.fstat(descriptor)
        return info.st_dev, info.st_ino


def atomic_replace(source: Path, target: Path, *, replace_open: bool = False) -> None:
    """原子发布同目录文件；Windows 在发布前刷新文件数据。"""
    if os.name == 'nt':
        from .windows_files import atomic_replace as windows_atomic_replace
        options = {'replace_open': True} if replace_open else {}
        windows_atomic_replace(source, target, **options)
    else:
        os.replace(source, target)


def fsync_directory(path: Path) -> None:
    """同步目录；Windows 无 POSIX 等价保证，仅验证目录。"""
    if os.name == 'nt':
        from .windows_files import fsync_directory as windows_fsync_directory
        windows_fsync_directory(path)
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
