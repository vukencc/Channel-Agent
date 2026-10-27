"""只读文件预览：通过目录句柄逐层打开，拒绝验证后的符号链接替换。"""
import itertools
import os
from pathlib import Path
import stat

from agent_core.core.files import opened_path


def directory_entries(root: Path, target: Path, *, expected_identity=None):
    entries = []
    with opened_path(root, target, directory=True, expected_identity=expected_identity) as descriptor:
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


def text_preview(root: Path, target: Path, *, expected_identity=None):
    with opened_path(root, target, expected_identity=expected_identity) as descriptor:
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            data = stream.read(262145)
    if b'\x00' in data:
        raise ValueError('此文件不是可预览文本')
    return {'content': data[:262144].decode('utf-8', errors='replace'), 'truncated': len(data) > 262144}
