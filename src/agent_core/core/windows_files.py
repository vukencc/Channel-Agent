"""Windows 工作区句柄：拒绝重解析点并在访问期间锁定祖先目录。"""
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import msvcrt
import os
from pathlib import Path
import re
import stat

_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_create = _kernel.CreateFileW
_create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_create.restype = wintypes.HANDLE
_close = _kernel.CloseHandle
_close.argtypes = [wintypes.HANDLE]
_close.restype = wintypes.BOOL

class _FileInfo(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME),
                ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
                ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]

_info = _kernel.GetFileInformationByHandle
_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
_info.restype = wintypes.BOOL
class _FileIdInfo(ctypes.Structure):
    _fields_ = [('volume', ctypes.c_ulonglong), ('file_id', ctypes.c_ubyte * 16)]

_id_info = _kernel.GetFileInformationByHandleEx
_id_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_id_info.restype = wintypes.BOOL


def _identity(handle: int) -> tuple[int, int]:
    info = _FileIdInfo()
    if not _id_info(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.volume, int.from_bytes(bytes(info.file_id), 'little')


_INVALID = ctypes.c_void_p(-1).value
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$', re.I)


def _validate(path: Path) -> None:
    text = str(path)
    if not re.match(r'^[A-Za-z]:[\\/]', text) or text.startswith(('\\\\', '//')):
        raise PermissionError('工作区必须为本地 Windows 绝对目录')
    for part in path.parts[1:]:
        if (part in ('', '.', '..') or part[-1:] in (' ', '.') or _RESERVED.match(part)
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)):
            raise PermissionError('路径分量无效')


def _open(path: Path, *, directory: bool, writable: bool = False,
          allow_replace: bool = False) -> tuple[int, tuple[int, int]]:
    if allow_replace and (directory or writable):
        raise ValueError('原子快照替换只适用于只读普通文件')
    # 祖先和普通工具句柄禁止删除共享；仅原子快照可替换最终只读文件。
    access = 0x80000000 | (0x40000000 if writable else 0)
    share = 0x1 | 0x2 | (0x4 if allow_replace else 0)
    handle = _create(str(path), access, share, None, 3,
                     0x00200000 | 0x02000000, None)
    if handle == _INVALID:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _FileInfo()
    if not _info(handle, ctypes.byref(info)):
        error = ctypes.get_last_error()
        _close(handle)
        raise ctypes.WinError(error)
    if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
        _close(handle)
        raise PermissionError('路径权限或类型已发生变化，拒绝预览')
    if not directory and info.links != 1:
        _close(handle)
        raise PermissionError('无法确认硬链接全部位于授权目录，拒绝打开文件')
    try:
        return handle, _identity(handle)
    except BaseException:
        _close(handle)
        raise


@contextmanager
def opened_path(root: Path, target: Path, *, directory=False, expected_identity=None,
                writable: bool = False, allow_replace: bool = False):
    if allow_replace and (directory or writable):
        raise ValueError('原子快照替换只适用于只读普通文件')
    root, target = Path(root), Path(target)
    _validate(root)
    _validate(target)
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise PermissionError('路径不属于当前工作区') from None
    handles = []
    fd = None
    try:
        current = Path(root.anchor)
        handle, identity = _open(current, directory=True)
        handles.append(handle)
        for part in root.parts[1:]:
            current /= part
            handle, identity = _open(current, directory=True)
            handles.append(handle)
        if expected_identity is not None and identity != tuple(expected_identity):
            raise PermissionError('工作区目录身份已变化，拒绝预览')
        for index, part in enumerate(parts):
            current /= part
            is_directory = directory or index < len(parts) - 1
            options = {'allow_replace': True} if allow_replace and not is_directory else {}
            handle, _ = _open(current, directory=is_directory, writable=writable and not is_directory, **options)
            handles.append(handle)
        if not parts and not directory:
            raise ValueError('只支持普通文本文件或目录预览')
        handle = handles.pop()
        try:
            access = os.O_RDWR if writable and not directory else os.O_RDONLY
            fd = msvcrt.open_osfhandle(handle, access | os.O_BINARY)
        except BaseException:
            _close(handle)
            raise
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise ValueError('只支持普通文本文件或目录预览')
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        for handle in reversed(handles):
            _close(handle)


def directory_identity(path: Path) -> tuple[int, int]:
    """以安全句柄取得与 stat 相同的设备和文件身份。"""
    with opened_path(path, path, directory=True) as fd:
        # Windows 的 fstat 不一定提供文件索引，直接查询原生句柄。
        return _identity(msvcrt.get_osfhandle(fd))


def atomic_replace(source: Path, target: Path) -> None:
    """以已打开的文件句柄原子替换同一目录内的目标。"""
    source, target = Path(source), Path(target)
    _validate(source)
    _validate(target)
    if source.parent != target.parent or source == target:
        raise PermissionError('原子替换仅支持同一目录内的不同文件')
    # 父目录和全部祖先在整个操作期间禁止删除共享。
    with opened_path(source.parent, source.parent, directory=True):
        handle = _create(str(source), 0x80000000 | 0x40000000 | 0x10000,
                         0x1 | 0x2, None, 3, 0x00200000, None)
        if handle == _INVALID:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _FileInfo()
            if not _info(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & (0x400 | 0x10) or info.links != 1:
                raise PermissionError('源文件类型或硬链接不安全')
            try:
                destination, _ = _open(target, directory=False)
            except FileNotFoundError:
                pass
            else:
                _close(destination)
            flush = _kernel.FlushFileBuffers
            flush.argtypes = [wintypes.HANDLE]
            flush.restype = wintypes.BOOL
            if not flush(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            name = str(target).encode('utf-16-le')
            class _RenameInfo(ctypes.Structure):
                _fields_ = [('replace', wintypes.BOOLEAN),
                            ('root', wintypes.HANDLE),
                            ('length', wintypes.DWORD),
                            ('name', ctypes.c_ubyte * (len(name) + 2))]
            rename = _RenameInfo()
            rename.replace = 1
            rename.root = None
            rename.length = len(name)
            rename.name[:len(name)] = name
            set_info = _kernel.SetFileInformationByHandle
            set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            set_info.restype = wintypes.BOOL
            if not set_info(handle, 3, ctypes.byref(rename), ctypes.sizeof(rename)):
                raise ctypes.WinError(ctypes.get_last_error())
            # 失败时替换可能已经发生，调用方不得假定原目标仍存在。
            if not flush(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            # Windows 没有等价于 POSIX 目录 fsync 的保证。
        finally:
            _close(handle)


def fsync_directory(path: Path) -> None:
    """Windows 不提供 POSIX 目录 fsync；验证目录并保持平台差异明确。"""
    with opened_path(path, path, directory=True):
        pass


def rename_noreplace(source: Path, target: Path) -> None:
    """锁定父目录并以源句柄原子移动，不覆盖目标。"""
    source, target = Path(source), Path(target)
    _validate(source)
    _validate(target)
    with opened_path(source.parent, source.parent, directory=True):
        with opened_path(target.parent, target.parent, directory=True):
            handle = _create(str(source), 0x10000 | 0x80, 0x1 | 0x2,
                             None, 3, 0x00200000 | 0x02000000, None)
            if handle == _INVALID:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                info = _FileInfo()
                if not _info(handle, ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if info.attributes & 0x400:
                    raise PermissionError('源路径不允许重解析点')
                name = str(target).encode('utf-16-le')
                class _RenameInfo(ctypes.Structure):
                    _fields_ = [('replace', wintypes.BOOLEAN),
                                ('root', wintypes.HANDLE),
                                ('length', wintypes.DWORD),
                                ('name', ctypes.c_ubyte * (len(name) + 2))]
                rename = _RenameInfo()
                rename.replace = 0
                rename.length = len(name)
                rename.name[:len(name)] = name
                set_info = _kernel.SetFileInformationByHandle
                set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
                set_info.restype = wintypes.BOOL
                if not set_info(handle, 3, ctypes.byref(rename), ctypes.sizeof(rename)):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                _close(handle)
