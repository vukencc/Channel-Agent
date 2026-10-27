"""网络额度耗尽后，拒绝连接不能绕过额度无限写宿主审计。"""
import json
import threading

from agent_core.tools.network_proxy import NetworkProxy
from agent_core.tools.sandbox import ToolContext, tool_context


def test_repeated_exhaustion_has_bounded_audit_without_losing_reason(tmp_path, sandbox_env):
    audit = tmp_path / 'network-audit.jsonl'
    proxy = NetworkProxy(max_bytes=1024, max_connections=1, timeout=1)
    with tool_context(ToolContext(sandbox_env, audit, lambda *_: True, threading.Event())):
        for _ in range(100):
            proxy._record_denial('connection_limit', '命令网络生命周期连接数超限')
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert 1 <= len(records) <= 8
    assert all(row['denial_code'] == 'connection_limit' for row in records)
    assert '连接数超限' in proxy.denial_summary()
