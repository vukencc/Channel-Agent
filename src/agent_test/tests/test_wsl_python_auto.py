"""WSL 自动解释器选择仅使用当前用户固定目录。"""
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from agent_core.tools import wsl_command_worker as worker

pytestmark = pytest.mark.skipif(os.name == "nt", reason="需要 POSIX 用户账户")


def test_auto_reexec_uses_account_home_and_preserves_venv_path(tmp_path, monkeypatch):
    executable = tmp_path / ".local/share/ChannelAgent/wsl-venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to("/usr/bin/python3")
    monkeypatch.setattr(worker.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    calls = []
    monkeypatch.setattr(worker.os, "execv", lambda path, argv: calls.append((path, argv)))
    worker._auto_reexec(["worker.py", "--auto-python"])
    assert calls == [(str(executable), [str(executable), "-I", "-B", "-u", "worker.py"])]


def test_auto_reexec_missing_uses_original_python_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/untrusted/home")
    monkeypatch.setattr(worker.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    calls = []
    monkeypatch.setattr(worker.os, "execv", lambda path, argv: calls.append((path, argv)))
    worker._auto_reexec(["worker.py", "--auto-python"])
    assert calls == [(worker.sys.executable, [worker.sys.executable, "-B", "-u", "worker.py"])]


def test_auto_reexec_fails_closed_when_selected_venv_exec_fails(tmp_path, monkeypatch):
    executable = tmp_path / ".local/share/ChannelAgent/wsl-venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to("/usr/bin/python3")
    monkeypatch.setattr(worker.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    def fail(path, argv):
        raise OSError("exec failed")
    monkeypatch.setattr(worker.os, "execv", fail)
    with pytest.raises(OSError, match="exec failed"):
        worker._auto_reexec(["worker.py", "--auto-python"])


def test_explicit_worker_has_no_dispatch(monkeypatch):
    monkeypatch.setattr(worker.os, "execv", lambda *_: pytest.fail("unexpected exec"))
    worker._auto_reexec(["worker.py"])


def test_missing_venv_reexec_restores_user_site_flags(tmp_path):
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text(
        "import importlib.util, sys, types\n"
        f"spec = importlib.util.spec_from_file_location('worker', {str(Path(worker.__file__))!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"module.pwd.getpwuid = lambda uid: types.SimpleNamespace(pw_dir={str(tmp_path)!r})\n"
        "if len(sys.argv) > 1: module._auto_reexec(sys.argv)\n"
        "else: print(sys.flags.isolated, sys.flags.no_user_site)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-u", str(bootstrap), "--auto-python"],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "0 0"
