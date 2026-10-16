"""pytest 公共夹具。"""
import os

import pytest

# 测试默认行为不受用户忽略的 .env 污染；显式启用功能的用例自行 monkeypatch。
# 必须在首次导入 config/tools 前设置，子进程也继承相同基线。
for name in ('AUTO_CONTINUE_TASKS', 'ENABLE_STRUCTURED_CONTEXT', 'ENABLE_TASK_PLANS',
             'ENABLE_ENVIRONMENT_PREPARE'):
    os.environ[name] = 'false'
os.environ['COMMAND_RUNTIME_PATHS'] = '{}'
os.environ['COMMAND_NODE_MEMORY_MB'] = '0'
os.environ['COMMAND_NETWORK'] = 'off'
os.environ['COMMAND_NETWORK_ALLOWLIST'] = '[]'
for name in ('ENVIRONMENT_PREPARE_NETWORK_BYTES', 'ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS',
             'ENVIRONMENT_PREPARE_NETWORK_TIMEOUT', 'ENVIRONMENT_PREPARE_FILE_MB',
             'ENVIRONMENT_PREPARE_CPU_SECONDS'):
    os.environ[name] = '0'
# Keep context tests on the established finite window regardless of a local .env.
# Tests for automatic or model-specific capacity opt in with monkeypatch.
os.environ['MODEL_CONTEXT_TOKENS'] = '0'
os.environ['MODEL_INPUT_TOKENS'] = '16000'
os.environ['MODEL_INPUT_CHARS'] = '64000'

from ai_agent_startup import config
from ai_agent_startup.tools import sandbox


@pytest.fixture(autouse=True)
def isolated_default_audit(tmp_path, monkeypatch):
    """未设置工具上下文的测试也不能污染用户主审计；子进程沿用隔离路径。"""
    path = tmp_path / 'default-audit.jsonl'
    monkeypatch.setattr(config, 'AUDIT_LOG', path)
    monkeypatch.setenv('AUDIT_LOG', str(path))


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    """把工具沙箱指向临时目录，审计日志放在沙箱外，默认「用户同意」。"""
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "SANDBOX_DIR", sandbox_dir)
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(sandbox, "confirmer", lambda prompt, timeout: True)
    return sandbox_dir
