"""真实 Linux worker 的原生侧协议回归；不等同 Windows 到 WSL 的端到端实测。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading

import pytest

from agent_core import config
from agent_core.tools import sandbox
from agent_core.tools import wsl_command as bridge


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实 Linux worker 协议；原生 Windows WSL 桥接另验")


@pytest.fixture
def worker_exchange(sandbox_env, tmp_path, monkeypatch):
    settings = {name: getattr(config, name) for name in bridge.CONFIG_KEYS}
    settings.update(COMMAND_TIMEOUT=2, CONFIRM_TIMEOUT=2, COMMAND_RUNTIME_PATHS={},
                    COMMAND_NETWORK="off", TOOL_ROOTS={})
    paths = {name: str(tmp_path / name.lower()) for name in ("PROJECT_ROOT", "AGENT_STATE_DIR", "AUDIT_LOG")}
    request = {"id": "real-worker-authority", "operation": "run_command",
               "arguments": {"command": "python3 -c \"from pathlib import Path; Path('effect').touch()\""},
               "root": str(sandbox_env), "config": settings, "policy": "full_access", "paths": paths,
               "execution_environment": "sandbox", "flow_workspace_scope": True}
    argv = ["/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8",
            "PYTHON_DOTENV_DISABLED=1", "AI_AGENT_PROJECT_ROOT=" + str(tmp_path / "worker-state"),
            sys.executable, "-B", "-u", str(Path(bridge.__file__).with_name("wsl_command_worker.py"))]
    stat = sandbox_env.stat()
    context = sandbox.ToolContext(sandbox_env, tmp_path / "native-audit.jsonl", lambda *_: True,
                                  threading.Event(), permission_policy="full_access",
                                  root_identity=(stat.st_dev, stat.st_ino), execution_environment="sandbox",
                                  flow_workspace_scope=True)
    return sandbox_env, argv, request, context


@pytest.mark.parametrize("failed_event", ["confirm", "command_started", "executed"])
def test_real_worker_audit_failure_reports_execution_truth(worker_exchange, monkeypatch, failed_event):
    workspace, argv, request, context = worker_exchange
    append = sandbox.append_audit
    failed = []
    acknowledged = []
    def fail_selected(path, line, **kwargs):
        entry = json.loads(line)
        event = entry.get("worker_event", {}).get("event")
        if event == failed_event:
            failed.append(event)
            raise OSError("controlled-native-worker-audit-failure")
        result = append(path, line, **kwargs)
        if event:
            acknowledged.append(event)
        return result
    monkeypatch.setattr(sandbox, "append_audit", fail_selected)
    with sandbox.tool_context(context):
        if failed_event == "executed":
            result = bridge._exchange(argv, request, context.cancelled)
            assert result.startswith("[失败]"), result
            assert "可能有副作用" in result and "[已拦截]" not in result, result
            assert (workspace / "effect").exists()
            assert "command_started" in acknowledged
        else:
            with pytest.raises(sandbox.SandboxError, match="审计"):
                bridge._exchange(argv, request, context.cancelled)
            assert not (workspace / "effect").exists()
            assert "command_started" not in acknowledged
    assert failed == [failed_event]


def test_real_worker_authority_denial_at_start_ack_prevents_side_effect(worker_exchange):
    workspace, argv, request, context = worker_exchange
    guard_calls = []
    @contextmanager
    def revoked_authority():
        guard_calls.append("checked-before-command-start-ack")
        raise sandbox.SandboxError("父级能力已撤销")
        yield  # pragma: no cover
    context.execution_guard = revoked_authority
    with sandbox.tool_context(context):
        with pytest.raises(sandbox.SandboxError, match="审计"):
            bridge._exchange(argv, request, context.cancelled)
    assert guard_calls == ["checked-before-command-start-ack"]
    assert not (workspace / "effect").exists()
    entries = [json.loads(line) for line in context.audit_path.read_text().splitlines()]
    assert any(entry.get("worker_event", {}).get("event") == "confirm" for entry in entries)
    assert not any(entry.get("worker_event", {}).get("event") == "command_started" for entry in entries)
