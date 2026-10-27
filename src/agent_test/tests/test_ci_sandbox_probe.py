"""CI 隔离探针不能跟随应用的本机执行默认值。"""
from pathlib import Path
import runpy

import pytest

from agent_core import config
from agent_core.tools import command
from agent_core.tools.sandbox import SandboxError


def test_ci_probe_does_not_report_local_execution_as_sandbox(monkeypatch, capsys):
    monkeypatch.setattr(config, 'COMMAND_EXECUTION_ENVIRONMENT', 'local')
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(True)
        raise SandboxError('controlled missing namespace')

    monkeypatch.setattr(command, 'isolated_command', unavailable)
    probe = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/ci_sandbox_probe.py'))
    with pytest.raises(SystemExit) as stopped:
        probe['main']()
    assert stopped.value.code == 2
    assert attempts == [True]
    output = capsys.readouterr()
    assert '实际隔离执行通过' not in output.out
    assert '真实沙箱测试不能运行' in output.err
