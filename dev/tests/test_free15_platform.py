import json
import os
import subprocess
import sys

import pytest

from tools.sandbox import SandboxError, isolated_command


def test_non_linux_commands_fail_closed_before_launcher_probe(monkeypatch):
    import tools.sandbox as sandbox
    import shutil
    monkeypatch.setattr(sandbox.sys, 'platform', 'darwin')
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('平台拒绝必须早于探测启动器')))
    with pytest.raises(SandboxError, match='Linux|WSL'):
        isolated_command(['/bin/sh', '-c', 'true'])


def test_real_process_file_lock_rejects_second_writer_and_releases(tmp_path):
    from core.file_lock import lock_file, unlock_file
    path = tmp_path / 'lock'
    script = ('from core.file_lock import lock_file; import sys; '
              'stream=open(sys.argv[1], "a+b"); lock_file(stream, blocking=False)')
    with path.open('a+b') as stream:
        lock_file(stream, blocking=False)
        denied = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, text=True)
        assert denied.returncode != 0 and 'BlockingIOError' in denied.stderr
        unlock_file(stream)
        accepted = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, text=True)
        assert accepted.returncode == 0, accepted.stderr


def test_platform_entry_requires_no_credentials_or_state_directory(tmp_path):
    result = subprocess.run([sys.executable, 'main.py', '--platform', '--state-dir', str(tmp_path / 'unused')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['command_backend'] == 'bubblewrap' if sys.platform == 'linux' else payload['command_backend'] is None
    assert not (tmp_path / 'unused').exists()


def test_windows_lock_adapter_contract_is_nonblocking_and_uses_first_byte(tmp_path, monkeypatch):
    import errno
    from types import SimpleNamespace
    from core.file_lock import _windows_lock
    calls = []
    def locking(descriptor, mode, count):
        calls.append((os.lseek(descriptor, 0, os.SEEK_CUR), mode, count))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=99, locking=locking))
    with (tmp_path / 'windows-contract.lock').open('a+b') as stream:
        _windows_lock(stream, False)
        assert calls == [(0, 99, 1)]
        def busy(*args):
            raise OSError(errno.EACCES, 'locked')
        monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=99, locking=busy))
        with pytest.raises(BlockingIOError):
            _windows_lock(stream, False)
