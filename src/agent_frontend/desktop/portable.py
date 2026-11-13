"""便携目录初始化；不得导入运行时配置。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import io
import os
from pathlib import Path
import shutil
import tempfile
import time

from agent_core.core.files import opened_path


class PortableError(Exception):
    def __init__(self, path: Path, operation: str, kind: str):
        self.path, self.operation, self.kind = Path(path), operation, kind
        super().__init__(f'{operation}: {path} ({kind})')


@dataclass(frozen=True)
class PortableLayout:
    root: Path
    config: Path
    state: Path
    workspaces: Path
    projects: Path
    raw: Path
    rag_cache: Path
    env_file: Path


def _error(path: Path, operation: str, exc: Exception) -> PortableError:
    cause = exc.__cause__ if isinstance(exc.__cause__, OSError) else exc
    if isinstance(cause, OSError) and (cause.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC) or getattr(cause, 'winerror', None) in (5, 19, 112)):
        return PortableError(path, operation, 'unwritable')
    return PortableError(path, operation, 'io')


def _check_directory(path: Path) -> None:
    try:
        with opened_path(path, path, directory=True):
            pass
    except (PermissionError, ValueError) as exc:
        if (isinstance(exc.__cause__, OSError) and exc.__cause__.errno == errno.EACCES) or getattr(exc, 'winerror', None) == 5:
            raise _error(path, '访问目录', exc) from exc
        raise PortableError(path, '检查目录', 'unsafe') from exc
    except OSError as exc:
        raise _error(path, '访问目录', exc) from exc


def _protect(path: Path) -> None:
    if os.name == 'nt':
        from agent_core.core.windows_security import protect_private_path
        protect_private_path(path)
    else:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _verify_private_directory(path: Path) -> None:
    try:
        with opened_path(path, path, directory=True) as fd:
            if os.name == 'nt':
                from agent_core.core.windows_security import verify_private_path
                verify_private_path(path)
            elif os.fstat(fd).st_mode & 0o077:
                raise PermissionError('目录权限不私有')
    except (OSError, ValueError) as exc:
        raise PortableError(path, '检查私有目录', 'unsafe') from exc


def _mkdir(path: Path, private: bool = False) -> None:
    if path.exists() or path.is_symlink():
        _check_directory(path)
        return
    if path.parent != path:
        _mkdir(path.parent)
    try:
        with opened_path(path.parent, path.parent, directory=True):
            try:
                path.mkdir(mode=0o700 if private else 0o777)
                created = True
            except FileExistsError:
                created = False
            _check_directory(path)
            if created and private:
                _protect(path)
    except PortableError:
        raise
    except OSError as exc:
        raise _error(path, '创建目录', exc) from exc


def _verify_env(path: Path) -> None:
    try:
        with opened_path(path.parent, path) as fd:
            if os.name == 'nt':
                from agent_core.core.windows_security import verify_private_path
                verify_private_path(path)
            elif os.fstat(fd).st_mode & 0o077:
                raise PermissionError('环境文件权限不私有')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024 or b'\x00' in content:
            raise ValueError('环境文件无效')
        from dotenv.parser import parse_stream
        if any(binding.error for binding in parse_stream(io.StringIO(content.decode('utf-8-sig')))):
            raise ValueError('环境文件格式无效')
    except (PermissionError, ValueError, UnicodeError) as exc:
        raise PortableError(path, '检查环境文件', 'invalid' if isinstance(exc, (ValueError, UnicodeError)) else 'unsafe') from exc
    except OSError as exc:
        raise _error(path, '读取环境文件', exc) from exc


@contextmanager
def _lock(root: Path):
    path = root / '.portable.lock'
    try:
        with opened_path(root, root, directory=True):
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
            try:
                fd = os.open(path, flags, 0o600)
                try:
                    os.write(fd, b'0')
                finally:
                    os.close(fd)
                _protect(path)
            except FileExistsError:
                pass
        with opened_path(root, path, writable=True) as fd:
            deadline = time.monotonic() + 10
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise PortableError(path, '等待初始化锁', 'io')
                    time.sleep(0.05)
            try:
                yield
            finally:
                if os.name == 'nt':
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
    except PortableError:
        raise
    except PermissionError as exc:
        if (exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
                or (exc.__cause__ and getattr(exc.__cause__, 'errno', None) == errno.EACCES)
                or getattr(exc, 'winerror', None) == 5):
            raise _error(path, '访问初始化锁', exc) from exc
        raise PortableError(path, '检查初始化锁', 'unsafe') from exc
    except OSError as exc:
        raise _error(path, '创建初始化锁', exc) from exc


def _create_env(root: Path) -> None:
    target = root / '.env'
    if target.exists() or target.is_symlink():
        _verify_env(target)
        return
    temp = None
    try:
        with opened_path(root, root, directory=True):
            fd, name = tempfile.mkstemp(prefix='.portable-env-', dir=root)
            temp = Path(name)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(b'# ChannelAgent portable configuration\nOPENCODE_API_KEY=\n')
                stream.flush()
                os.fsync(stream.fileno())
            _protect(temp)
            if os.name == 'nt':
                from agent_core.core.windows_files import rename_noreplace
                rename_noreplace(temp, target, file_only=True)
            else:
                os.link(temp, target, follow_symlinks=False)
                temp.unlink()
            temp = None
        _verify_env(target)
    except FileExistsError:
        _verify_env(target)
    except PortableError:
        raise
    except OSError as exc:
        raise _error(target, '创建环境文件', exc) from exc
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def initialize_layout(root: Path) -> PortableLayout:
    root = Path(os.path.abspath(os.path.expanduser(str(root))))
    _mkdir(root)
    layout = PortableLayout(root, root / 'config', root / 'data/state', root / 'data/workspaces',
                            root / 'data/projects/default', root / 'data/raw', root / 'cache/rag', root / '.env')
    with _lock(root):
        directories = (root, layout.config, root / 'data', layout.state, layout.workspaces, root / 'data/projects',
                       layout.projects, layout.raw, root / 'cache', layout.rag_cache, root / 'cache/huggingface',
                       root / 'cache/huggingface/hub', root / 'cache/huggingface/modules',
                       root / 'cache/sentence-transformers', root / 'cache/fastembed',
                       root / 'cache/torch', root / 'cache/tmp', root / 'logs')
        for path in directories[1:]:
            _mkdir(path, private=True)
        _verify_private_directory(layout.config)
        _verify_private_directory(layout.state)
        _create_env(root)
        for path in directories:
            probe = None
            try:
                with opened_path(path, path, directory=True):
                    fd, probe = tempfile.mkstemp(prefix='.portable-write-', dir=path)
                    os.close(fd)
                    os.unlink(probe)
                    probe = None
            except OSError as exc:
                raise _error(path, '写入目录', exc) from exc
            finally:
                if probe is not None:
                    try:
                        os.unlink(probe)
                    except OSError:
                        pass
    return layout


def configure_environment(layout: PortableLayout) -> None:
    os.environ['AI_AGENT_PROJECT_ROOT'] = str(layout.root)
    os.environ['AI_AGENT_PORTABLE'] = '1'
    for name, path in {'HF_HOME': layout.root / 'cache/huggingface', 'TORCH_HOME': layout.root / 'cache/torch',
                       'XDG_CACHE_HOME': layout.root / 'cache', 'TEMP': layout.root / 'cache/tmp',
                       'TMP': layout.root / 'cache/tmp',
                       'HF_HUB_CACHE': layout.root / 'cache/huggingface/hub',
                       'HUGGINGFACE_HUB_CACHE': layout.root / 'cache/huggingface/hub',
                       'TRANSFORMERS_CACHE': layout.root / 'cache/huggingface/hub',
                       'HF_MODULES_CACHE': layout.root / 'cache/huggingface/modules',
                       'SENTENCE_TRANSFORMERS_HOME': layout.root / 'cache/sentence-transformers',
                       'FASTEMBED_CACHE_PATH': layout.root / 'cache/fastembed'}.items():
        os.environ[name] = str(path)
    tempfile.tempdir = str(layout.root / 'cache/tmp')


def install_packaged_resources(layout: PortableLayout, source: Path) -> None:
    """复制内嵌公开资料，保留目标中已有的文件。"""
    source = Path(source)
    if not source.is_dir():
        raise PortableError(source, '读取内嵌资源', 'invalid')
    for relative in ('configuration.example.env', 'LICENSE.txt', '使用说明.txt', 'WSL/requirements.txt'):
        if not (source / relative).is_file():
            raise PortableError(source / relative, '读取内嵌资源', 'invalid')
    if not (source / 'THIRD_PARTY_LICENSES').is_dir():
        raise PortableError(source / 'THIRD_PARTY_LICENSES', '读取内嵌资源', 'invalid')
    with _lock(layout.root):
        for item in sorted(source.rglob('*')):
            relative = item.relative_to(source)
            target = layout.root / relative
            if item.is_symlink():
                raise PortableError(item, '检查内嵌资源', 'unsafe')
            if item.is_dir():
                _mkdir(target)
                continue
            _mkdir(target.parent)
            if target.exists() or target.is_symlink():
                try:
                    with opened_path(layout.root, target):
                        pass
                except (OSError, ValueError) as exc:
                    raise PortableError(target, '检查已有资料', 'unsafe') from exc
                continue
            temporary = None
            try:
                with opened_path(target.parent, target.parent, directory=True):
                    fd, name = tempfile.mkstemp(prefix='.portable-resource-', dir=target.parent)
                    temporary = Path(name)
                    with os.fdopen(fd, 'wb') as output, item.open('rb') as input_file:
                        shutil.copyfileobj(input_file, output)
                        output.flush()
                        os.fsync(output.fileno())
                    if os.name == 'nt':
                        from agent_core.core.windows_files import rename_noreplace
                        rename_noreplace(temporary, target, file_only=True)
                    else:
                        os.link(temporary, target, follow_symlinks=False)
                        temporary.unlink()
                    temporary = None
            except FileExistsError:
                with opened_path(layout.root, target):
                    pass
            except OSError as exc:
                raise _error(target, '复制内嵌资料', exc) from exc
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
