import os
from pathlib import Path

import pytest

from agent_core.core.files import opened_path

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows 原生句柄测试')


def test_opened_path_reads_file_and_checks_identity(tmp_path: Path):
    target = tmp_path / 'sample.txt'
    target.write_text('hello', encoding='utf-8')
    info = tmp_path.stat()
    with opened_path(tmp_path, target, expected_identity=(info.st_dev, info.st_ino)) as fd:
        assert os.read(fd, 5) == b'hello'
    with pytest.raises(PermissionError):
        with opened_path(tmp_path, target, expected_identity=(info.st_dev, info.st_ino + 1)):
            pass


def test_directory_identity_matches_stat(tmp_path: Path):
    from agent_core.core.files import directory_identity
    info = tmp_path.stat()
    assert directory_identity(tmp_path) == (info.st_dev, info.st_ino)
    with opened_path(tmp_path, tmp_path, directory=True) as fd:
        assert os.fstat(fd)


@pytest.mark.parametrize('name', ['NUL.txt', 'file:stream', 'trailing.', 'bad?.txt'])
def test_unsafe_names_rejected(tmp_path: Path, name: str):
    with pytest.raises(PermissionError):
        with opened_path(tmp_path, tmp_path / name):
            pass


def test_reparse_directory_rejected(tmp_path: Path):
    import subprocess
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('secret', encoding='utf-8')
    root = tmp_path / 'root'
    root.mkdir()
    link = root / 'junction'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(PermissionError):
            with opened_path(root, link / 'secret'):
                pass
    finally:
        os.rmdir(link)


def test_ancestor_cannot_be_renamed_while_open(tmp_path: Path):
    root = tmp_path / 'root'
    sub = root / 'sub'
    sub.mkdir(parents=True)
    target = sub / 'file'
    target.write_text('ok', encoding='utf-8')
    with opened_path(root, target) as fd:
        with pytest.raises(OSError):
            sub.rename(root / 'moved')
        assert os.read(fd, 2) == b'ok'
    sub.rename(root / 'moved')


def test_unc_and_device_paths_rejected(tmp_path: Path):
    for path in (Path(r'\\server\share\file'), Path(r'\\?\C:\file')):
        with pytest.raises(PermissionError):
            with opened_path(tmp_path, path):
                pass


def test_atomic_replace_preserves_content_and_syncs(tmp_path: Path):
    from agent_core.core.files import atomic_replace, fsync_directory
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_text('new', encoding='utf-8')
    target.write_text('old', encoding='utf-8')
    atomic_replace(source, target)
    fsync_directory(tmp_path)
    assert target.read_text(encoding='utf-8') == 'new'
    assert not source.exists()


def test_atomic_replace_missing_source_preserves_target(tmp_path: Path):
    from agent_core.core.files import atomic_replace
    target = tmp_path / 'target'
    target.write_text('old', encoding='utf-8')
    with pytest.raises(OSError):
        atomic_replace(tmp_path / 'missing', target)
    assert target.read_text(encoding='utf-8') == 'old'


def test_atomic_replace_rejects_hardlinked_source(tmp_path: Path):
    from agent_core.core.files import atomic_replace
    source, link, target = (tmp_path / n for n in ('source', 'link', 'target'))
    source.write_text('new', encoding='utf-8')
    os.link(source, link)
    target.write_text('old', encoding='utf-8')
    with pytest.raises(PermissionError):
        atomic_replace(source, target)
    assert source.read_text(encoding='utf-8') == 'new'
    assert target.read_text(encoding='utf-8') == 'old'


def test_atomic_replace_rejects_directory_target(tmp_path: Path):
    from agent_core.core.files import atomic_replace
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_text('new', encoding='utf-8')
    target.mkdir()
    with pytest.raises(PermissionError):
        atomic_replace(source, target)
    assert source.read_text(encoding='utf-8') == 'new'
    assert target.is_dir()


def test_atomic_replace_rejects_reparse_target(tmp_path: Path):
    from agent_core.core.files import atomic_replace
    source, outside, target = (tmp_path / n for n in ('source', 'outside', 'target'))
    source.write_text('new', encoding='utf-8')
    outside.write_text('outside', encoding='utf-8')
    try:
        os.symlink(outside, target)
    except OSError as exc:
        pytest.skip(f'无法创建符号链接: {exc}')
    with pytest.raises(PermissionError):
        atomic_replace(source, target)
    assert outside.read_text(encoding='utf-8') == 'outside'
    assert source.read_text(encoding='utf-8') == 'new'


def test_atomic_replace_long_path_uses_exact_destination_name(tmp_path: Path):
    import uuid
    from agent_core.core.files import atomic_replace
    parent = tmp_path / ('nested_' + 'x' * 72)
    parent.mkdir()
    target = parent / (uuid.uuid4().hex + '.json')
    source = parent / ('temporary_' + uuid.uuid4().hex)
    source.write_text('data', encoding='utf-8')
    atomic_replace(source, target)
    assert target.read_text(encoding='utf-8') == 'data'
    assert sorted(path.name for path in parent.iterdir()) == [target.name]


@pytest.mark.parametrize('is_directory', [False, True])
def test_windows_rename_noreplace_moves_file_and_directory(tmp_path: Path, is_directory: bool):
    from agent_core.tools.file_extras import _rename_no_replace
    source, target = tmp_path / 'source', tmp_path / 'target'
    if is_directory:
        source.mkdir()
        (source / 'child').write_text('content', encoding='utf-8')
    else:
        source.write_text('content', encoding='utf-8')
    _rename_no_replace(source, target)
    assert not source.exists()
    assert (target / 'child' if is_directory else target).read_text(encoding='utf-8') == 'content'


def test_windows_rename_noreplace_does_not_overwrite(tmp_path: Path):
    from agent_core.tools.file_extras import _rename_no_replace
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_text('source', encoding='utf-8')
    target.write_text('target', encoding='utf-8')
    with pytest.raises(OSError):
        _rename_no_replace(source, target)
    assert source.read_text(encoding='utf-8') == 'source'
    assert target.read_text(encoding='utf-8') == 'target'


def test_windows_rename_noreplace_between_parents(tmp_path: Path):
    from agent_core.tools.file_extras import _rename_no_replace
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    source, target = first / 'source', second / 'target'
    source.write_text('new', encoding='utf-8')
    _rename_no_replace(source, target)
    assert not source.exists()
    assert target.read_text(encoding='utf-8') == 'new'


def test_windows_rename_noreplace_rejects_junction_source(tmp_path: Path):
    import subprocess
    from agent_core.tools.file_extras import _rename_no_replace
    original, source, target = tmp_path / 'original', tmp_path / 'junction', tmp_path / 'target'
    original.mkdir()
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(source), str(original)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(PermissionError):
            _rename_no_replace(source, target)
        assert original.is_dir()
        assert not target.exists()
    finally:
        os.rmdir(source)
