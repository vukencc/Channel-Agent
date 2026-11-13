"""分发清单应覆盖全部文件并明确拒绝可跳出成品目录的链接。"""
import hashlib
import json
from pathlib import Path

import pytest

from agent_test.scripts.build_windows import file_manifest, source_root
from agent_test.scripts import build_windows


def test_distribution_manifest_hashes_nested_manifests_and_excludes_only_itself(tmp_path):
    nested = tmp_path / 'library'
    nested.mkdir()
    payload = b'packaged-library-data'
    (nested / 'manifest.json').write_bytes(payload)
    (tmp_path / 'manifest.json').write_text('{}', encoding='utf-8')
    records = file_manifest(tmp_path)
    assert records == [{'path': 'library/manifest.json', 'size': len(payload),
                        'sha256': hashlib.sha256(payload).hexdigest()}]
    assert str(tmp_path) not in json.dumps(records)


def test_distribution_manifest_rejects_links(tmp_path, monkeypatch):
    file = tmp_path / 'injected'
    file.write_text('outside-reference', encoding='utf-8')
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path == file or original(path))
    with pytest.raises(ValueError, match='符号链接'):
        file_manifest(tmp_path)


def test_build_source_root_is_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = source_root()
    assert (root / 'src/agent_frontend/desktop/launcher.py').is_file()
    assert (root / 'uv.lock').is_file()


def test_code_verification_tolerates_packager_paths_but_detects_stale_functions():
    original = compile('def run():\n    return 1\n', '/checkout/app.py', 'exec', dont_inherit=True)
    moved = compile('\n\ndef run():\n    return 1\n', 'frozen/app.py', 'exec', dont_inherit=True)
    stale = compile('def run():\n    return 2\n', 'frozen/app.py', 'exec', dont_inherit=True)
    assert build_windows.equivalent_code(original, moved)
    assert not build_windows.equivalent_code(original, stale)


def test_windows_system_icu_is_not_shadowed_by_foreign_tool_binaries():
    binaries = [
        ('icuuc.dll', 'foreign-tool/icuuc.dll', 'BINARY'),
        ('PySide6\\ICUUC.DLL', 'another-tool/icuuc.dll', 'BINARY'),
        ('Qt6Core.dll', 'qt-library.dll', 'BINARY'),
        ('msvcp140.dll', 'vc-runtime.dll', 'BINARY'),
    ]
    assert build_windows.windows_dependency_binaries(binaries) == binaries[2:]
    assert len(binaries) == 4


def test_build_environment_ignores_unrelated_developer_tool_paths(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', 'foreign-tool/poppler/bin')
    environment = build_windows.build_environment(tmp_path, tmp_path / 'icon.ico')
    assert 'foreign-tool' not in environment['PATH']
    assert 'System32' in environment['PATH']
    assert build_windows.os.environ['PATH'] == 'foreign-tool/poppler/bin'


def test_orphaned_icu_data_is_removed_but_versioned_icu_dependencies_are_preserved():
    binaries = [
        ('Qt6Core.dll', 'Qt6Core.dll', 'BINARY'),
        ('icudt78.dll', 'icudt78.dll', 'BINARY'),
    ]
    assert build_windows.windows_dependency_binaries(binaries) == binaries[:1]
    versioned = [*binaries, ('icuuc78.dll', 'icuuc78.dll', 'BINARY')]
    assert build_windows.windows_dependency_binaries(versioned) == versioned


def test_distribution_preserves_licenses_without_collecting_package_configuration(tmp_path, monkeypatch):
    package = tmp_path / 'installed'
    package.mkdir()
    (package / 'LICENSE.txt').write_text('Dependency copyright and permission', encoding='utf-8')
    (package / '.env').write_text('PRIVATE_SETTING=do-not-distribute', encoding='utf-8')
    class Distribution:
        metadata = {'Name': 'sample-library'}
        version = '1.0'
        files = [Path('LICENSE.txt'), Path('.env')]
        def locate_file(self, relative):
            return package / relative
    monkeypatch.setattr(build_windows.importlib.metadata, 'distributions', lambda: [Distribution()])
    output = tmp_path / 'output'
    output.mkdir()
    build_windows.copy_dependency_licenses(output)
    notices = output / 'THIRD_PARTY_LICENSES'
    assert (notices / 'sample-library-1.0/LICENSE.txt').read_text() == 'Dependency copyright and permission'
    assert not list(output.rglob('.env'))
    assert 'LESSER GENERAL PUBLIC LICENSE' in (notices / 'Qt-LGPL/LGPL-3.0-only.txt').read_text()


def test_onefile_is_default_and_onedir_is_explicit():
    import inspect
    assert inspect.signature(build_windows.build).parameters["layout"].default == "onefile"
    spec = (source_root() / "src/agent_test/packaging/windows/ChannelAgent.spec").read_text(encoding="utf-8")
    assert "CHANNEL_AGENT_BUILD_LAYOUT" in spec
    assert "a.binaries, a.datas" in spec


def test_preflight_reports_missing_packaging_extras(monkeypatch):
    monkeypatch.setattr(build_windows.importlib.util, "find_spec", lambda name: None if name == "docx" else object())
    with pytest.raises(SystemExit, match="documents.*vision"):
        build_windows.check_build_dependencies()
