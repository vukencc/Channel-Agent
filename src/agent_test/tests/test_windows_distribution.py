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


def test_single_exe_smoke_keeps_distribution_archive_name(tmp_path, monkeypatch):
    """受控子进程替身仅验证组装命名，不作为真实 exe 自检证据。"""
    from types import SimpleNamespace
    bundle = tmp_path / 'ChannelAgent.exe'
    bundle.write_bytes(b'controlled-package-input')
    resources = tmp_path / 'resources'
    resources.mkdir()
    (resources / 'LICENSE.txt').write_text('controlled public resource', encoding='utf-8')
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.setattr(build_windows, 'verify_application_code', lambda path: 1)
    monkeypatch.setattr(build_windows, 'verify_onefile_resources', lambda *args: 1)
    monkeypatch.setattr(build_windows.importlib.metadata, 'version', lambda name: 'controlled-metadata')
    monkeypatch.setattr(build_windows.subprocess, 'check_output',
                        lambda arguments, **kwargs: '0' * 40 if arguments[1] == 'rev-parse' else '')

    def controlled_smoke(arguments, **kwargs):
        executable = Path(arguments[0])
        assert [path.name for path in executable.parent.iterdir()] == ['ChannelAgent.exe']
        assert kwargs['timeout'] == 600
        assert 'AI_AGENT_PROJECT_ROOT' not in kwargs['env']
        assert 'CONTEXT_SUPERVISOR_ENV_FILE' not in kwargs['env']
        for relative in ('config', 'data/state', 'cache/rag', 'logs'):
            (executable.parent / relative).mkdir(parents=True, exist_ok=True)
        (executable.parent / '.env').write_bytes(b'# controlled initialization\n')
        Path(arguments[-1]).write_text('{"ok":true}', encoding='utf-8')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_windows.subprocess, 'run', controlled_smoke)
    destination = build_windows.assemble(bundle, tmp_path / 'output', work=work,
                                          uv='unused-controlled-uv', resources=resources)
    assert (destination.parent / f'{destination.name}.zip').is_file()
    assert (destination.parent / f'{destination.name}.zip.sha256').is_file()
    assert not (destination.parent / 'CONTEXT_SUPERVISOR_ENV_FILE.zip').exists()
    standalone = destination.parent / 'ChannelAgent-0.6.0-windows-x64.exe'
    assert standalone.read_bytes() == (destination / 'ChannelAgent.exe').read_bytes()
    digest = hashlib.sha256(standalone.read_bytes()).hexdigest()
    assert standalone.with_suffix('.exe.sha256').read_text() == f'{digest}  {standalone.name}\n'


@pytest.mark.parametrize('suffix', ['.exe', '.exe.sha256'])
def test_standalone_asset_never_overwrites_existing_file(tmp_path, suffix):
    output = tmp_path / 'output'
    output.mkdir()
    existing = output / f'ChannelAgent-0.6.0-windows-x64{suffix}'
    existing.write_bytes(b'existing asset')
    bundle = tmp_path / 'ChannelAgent.exe'
    bundle.write_bytes(b'controlled input')
    with pytest.raises(FileExistsError):
        build_windows.assemble(bundle, output, work=tmp_path, uv='unused')
    assert existing.read_bytes() == b'existing asset'
