"""原生只读预览；所有读取持有 Core 校验后的目录句柄。"""
import itertools
import os
from pathlib import Path
import stat

from agent_core.api import opened_path


def preview(root: Path, relative: str, *, directory: bool, identity=None) -> dict:
    path = Path(relative or '.')
    if path.is_absolute() or '..' in path.parts or relative.startswith('@'):
        raise PermissionError('文件预览只访问当前项目内的相对路径。')
    target = root / path
    if directory:
        entries = []
        with opened_path(root, target, directory=True, expected_identity=identity) as descriptor:
            with os.scandir(descriptor) as scan:
                for item in itertools.islice(scan, 500):
                    if item.is_symlink():
                        continue
                    try:
                        info = item.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        continue
                    entries.append({'name': item.name, 'path': str((target / item.name).relative_to(root)),
                                    'is_dir': stat.S_ISDIR(info.st_mode), 'size': info.st_size})
        return {'path': str(path), 'entries': sorted(entries, key=lambda row: (not row['is_dir'], row['name'].casefold()))}
    with opened_path(root, target, expected_identity=identity) as descriptor:
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            data = stream.read(262145)
    if b'\x00' in data:
        raise ValueError('此文件不是可预览文本。')
    return {'path': str(path), 'content': data[:262144].decode('utf-8', errors='replace'), 'truncated': len(data) > 262144}
