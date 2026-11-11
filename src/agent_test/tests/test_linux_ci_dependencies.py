"""Linux CI 镜像回退和 APT 失败传播测试。"""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
ACTION = ROOT / ".github/actions/setup-linux-system/action.yml"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux shell 与 APT 配置回归")


def run_action(tmp_path: Path, reachable: str, apt_failure: str = "", stale: bool = False) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """在临时目录执行真实 shell，使用桩命令替代网络和系统 APT。"""
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    script = action["runs"]["steps"][0]["run"]
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "ubuntu.sources"
    source.write_text("URIs: mirror+file:/etc/apt/apt-mirrors.txt http://security.ubuntu.com/ubuntu http://azure.archive.ubuntu.com/ubuntu https://example.org/ubuntu\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n", encoding="utf-8")
    script = script.replace("/etc/apt/sources.list.d/", f"{sources}/").replace("/etc/apt/sources.list", str(tmp_path / "sources.list"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    for name, body in {
        "sudo": 'exec "$@"',
        "curl": 'echo "curl $*" >> "$CALL_LOG"; case "$*" in *"$REACHABLE"*) test -n "$REACHABLE" || exit 1;; *) exit 1;; esac; while test "$1" != "-o"; do shift; done; date -u -d "$PROBE_DATE" "+Date: %a, %d %b %Y %H:%M:%S UTC" > "$2"',
        "apt-get": 'echo "apt-get $*" >> "$CALL_LOG"; case "$*" in *"$APT_FAILURE"*) test -z "$APT_FAILURE";; esac',
    }.items():
        command = bin_dir / name
        command.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        command.chmod(0o755)
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CALL_LOG": str(log), "REACHABLE": reachable, "APT_FAILURE": apt_failure, "PROBE_DATE": "90 days ago" if stale else "now"}
    result = subprocess.run(["bash", "-e", "-c", script], env=env, text=True, capture_output=True, check=False)
    return result, log.read_text(encoding="utf-8") if log.exists() else "", source.read_text(encoding="utf-8")


def test_fallback_preserves_signing_and_installs(tmp_path: Path) -> None:
    result, calls, source = run_action(tmp_path, "is.archive.ubuntu.com")
    assert result.returncode == 0, result.stderr
    assert "archive.ubuntu.com" in calls and "is.archive.ubuntu.com" in calls
    assert "https://is.archive.ubuntu.com/ubuntu" in source
    assert "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg" in source
    assert source.count("https://is.archive.ubuntu.com/ubuntu") == 3
    assert "https://example.org/ubuntu" in source
    assert "APT::Update::Error-Mode=any update" in calls
    assert "install -y bubblewrap util-linux libegl1 libopengl0" in calls


def test_all_mirrors_fail_before_apt(tmp_path: Path) -> None:
    result, calls, source = run_action(tmp_path, "")
    assert result.returncode != 0
    assert "apt-get" not in calls
    assert "mirror+file:" in source


def test_stale_security_metadata_is_rejected(tmp_path: Path) -> None:
    result, calls, source = run_action(tmp_path, "is.archive.ubuntu.com", stale=True)
    assert result.returncode != 0
    assert "apt-get" not in calls
    assert "mirror+file:" in source


@pytest.mark.parametrize("failure", [" update", " install "])
def test_apt_failure_is_propagated(tmp_path: Path, failure: str) -> None:
    result, calls, _ = run_action(tmp_path, "archive.ubuntu.com", failure)
    assert result.returncode != 0
    if failure == " update":
        assert " install " not in calls
