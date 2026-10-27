#!/usr/bin/python3
"""Build narrowly scoped, read-only Linux runtime mounts from trusted settings.

This file also supplies the two fixed npm/npx launchers inside the namespace.
It intentionally imports only the standard library so that launcher execution
does not import application configuration or expose any host credentials.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import platform
import stat
import sys
from typing import Mapping, Sequence


_RUNTIME_ROOT = '/run/agent-runtime'
_RUNTIME_BIN = _RUNTIME_ROOT + '/bin'
_NPM_ROOT = _RUNTIME_ROOT + '/lib/node_modules/npm'
_PRIVATE_COMPONENTS = frozenset({'.ssh', '.aws', '.azure', '.gnupg', '.config',
                                 '.codex', '.agent', '.git', '.env'})
_SYSTEM_PRIVATE = (Path('/etc'), Path('/proc'), Path('/sys'), Path('/dev'), Path('/run'))
_ELF_MACHINES = {'x86_64': (2, 62), 'amd64': (2, 62), 'aarch64': (2, 183),
                 'arm64': (2, 183), 'i386': (1, 3), 'i686': (1, 3),
                 'armv7l': (1, 40), 'riscv64': (2, 243), 'ppc64le': (2, 21),
                 's390x': (2, 22)}


class RuntimeBindingError(ValueError):
    """An explicit runtime could not safely be exposed to the namespace."""


@dataclass(frozen=True)
class RuntimeBindings:
    mount_args: list[str]
    environment: dict[str, str]


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _source_path(value: str, protected: Sequence[Path]) -> Path:
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or not Path(value).is_absolute()):
        raise RuntimeBindingError('COMMAND_RUNTIME_PATHS 仅接受 Linux 绝对路径，不能绑定 Windows 程序')
    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
        if candidate != resolved:
            raise RuntimeBindingError('运行时源路径含符号链接，拒绝绑定')
        if (resolved in {Path('/'), Path('/home'), Path('/root'), Path.home().resolve()}
                or any(part in _PRIVATE_COMPONENTS or part.startswith('.env.') for part in resolved.parts)
                or any(resolved.is_relative_to(root) for root in _SYSTEM_PRIVATE)
                or any(_overlaps(resolved, root) for root in protected)):
            raise RuntimeBindingError('运行时源路径与工作区、状态或私有配置重叠，拒绝绑定')
    except (OSError, RuntimeError) as exc:
        raise RuntimeBindingError('运行时源路径不存在或无法安全读取，拒绝绑定') from exc
    return resolved


def _elf_executable(path: Path, protected: Sequence[Path]) -> Path:
    path = _source_path(str(path), protected)
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or not mode & 0o111:
            raise RuntimeBindingError('运行时必须是可执行的普通 ELF 文件')
        with path.open('rb') as source:
            header = source.read(64)
    except OSError as exc:
        raise RuntimeBindingError('无法读取运行时 ELF 文件，拒绝绑定') from exc
    if len(header) < 52 or header[:4] != b'\x7fELF' or header[5] not in (1, 2):
        raise RuntimeBindingError('运行时必须是 Linux ELF 文件，不能使用 Windows PE 或脚本')
    byteorder = 'little' if header[5] == 1 else 'big'
    expected = _ELF_MACHINES.get(platform.machine().lower())
    if (expected is None or (header[4], int.from_bytes(header[18:20], byteorder)) != expected
            or byteorder != sys.byteorder):
        raise RuntimeBindingError('运行时 ELF 架构与当前 Linux 系统不匹配')
    if int.from_bytes(header[16:18], byteorder) not in (2, 3) or header[7] not in (0, 3):
        raise RuntimeBindingError('运行时必须是 Linux ELF 可执行文件')
    return path


def _npm_distribution(path: Path, protected: Sequence[Path]) -> Path:
    path = _source_path(str(path), protected)
    if not path.is_dir():
        raise RuntimeBindingError('Node 绑定缺少 lib/node_modules/npm 发行目录')
    def fail_walk(error: OSError) -> None:
        raise error
    try:
        for entry in ('bin/npm-cli.js', 'bin/npx-cli.js'):
            if not (path / entry).is_file():
                raise RuntimeBindingError('Node 绑定缺少 npm/npx 的固定入口')
        for directory, directories, files in os.walk(path, followlinks=False, onerror=fail_walk):
            for name in [*directories, *files]:
                entry = Path(directory) / name
                mode = entry.lstat().st_mode
                if stat.S_ISLNK(mode):
                    target = entry.resolve(strict=True)
                    if not target.is_relative_to(path):
                        raise RuntimeBindingError('npm 发行目录含指向外部的符号链接，拒绝绑定')
                    if not (target.is_file() or target.is_dir()):
                        raise RuntimeBindingError('npm 发行目录含非普通文件，拒绝绑定')
                elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise RuntimeBindingError('npm 发行目录含非普通文件，拒绝绑定')
    except (OSError, RuntimeError) as exc:
        raise RuntimeBindingError('npm 发行目录无法完整验证，拒绝绑定') from exc
    return path


def build_runtime_bindings(mapping: Mapping[str, str], *, workspace: Path,
                           protected_paths: Sequence[Path] = ()) -> RuntimeBindings:
    """Validate configured sources and return only fixed runtime mount targets.

    Node accepts a distribution prefix; uv accepts one native executable file.
    No host PATH discovery, whole home binding, or tool-supplied mount is used.
    """
    if not isinstance(mapping, dict) or any(name not in {'node', 'uv'} for name in mapping):
        raise RuntimeBindingError('COMMAND_RUNTIME_PATHS 当前仅支持 node 发行前缀和 uv ELF 文件')
    if not mapping:
        return RuntimeBindings([], {})
    try:
        protected = tuple(Path(path).resolve() for path in (workspace, *protected_paths) if path is not None)
        mounts = ['--dir', _RUNTIME_ROOT, '--dir', _RUNTIME_BIN]
        if 'node' in mapping:
            prefix = _source_path(mapping['node'], protected)
            if not prefix.is_dir():
                raise RuntimeBindingError('node 运行时需要包含 bin/node 的发行目录前缀')
            node = _elf_executable(prefix / 'bin/node', protected)
            npm = _npm_distribution(prefix / 'lib/node_modules/npm', protected)
            launcher = Path(__file__).resolve()
            if not launcher.is_file() or not launcher.stat().st_mode & 0o111:
                raise RuntimeBindingError('可信 npm/npx 启动器不可执行，拒绝绑定')
            mounts += ['--dir', _RUNTIME_ROOT + '/lib', '--dir', _RUNTIME_ROOT + '/lib/node_modules',
                       '--ro-bind', str(node), _RUNTIME_BIN + '/node',
                       '--ro-bind', str(npm), _NPM_ROOT,
                       '--ro-bind', str(launcher), _RUNTIME_BIN + '/npm',
                       '--ro-bind', str(launcher), _RUNTIME_BIN + '/npx']
        if 'uv' in mapping:
            uv = _elf_executable(_source_path(mapping['uv'], protected), protected)
            mounts += ['--ro-bind', str(uv), _RUNTIME_BIN + '/uv']
    except (OSError, TypeError, RuntimeError) as exc:
        raise RuntimeBindingError('运行时绑定验证失败，拒绝执行') from exc
    return RuntimeBindings(mounts, {'PATH': _RUNTIME_BIN + ':/usr/local/bin:/usr/bin:/bin'})


def _launch_package_manager() -> None:
    name = Path(sys.argv[0]).name
    if name not in {'npm', 'npx'}:
        raise SystemExit('Only the fixed npm/npx sandbox launchers may execute this module')
    os.execv(_RUNTIME_BIN + '/node', [_RUNTIME_BIN + '/node',
             _NPM_ROOT + '/bin/' + name + '-cli.js', *sys.argv[1:]])


if __name__ == '__main__':
    _launch_package_manager()
