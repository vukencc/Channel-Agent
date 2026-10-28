"""没有显式工具上下文的测试也必须将审计写入临时目录。"""
import json

from agent_core import config
from agent_core.tools.sandbox import audit


def test_default_audit_does_not_write_repository_logs(tmp_path):
    # 先检查边界，失败时也不能向用户日志追加探测记录。
    assert config.AUDIT_LOG.is_relative_to(tmp_path)
    audit('test_audit_isolation_probe')
    entries = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    assert entries[-1]['event'] == 'test_audit_isolation_probe'
