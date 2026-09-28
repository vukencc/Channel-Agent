"""pytest 公共夹具。"""
import pytest

from ai_agent_startup import config
from ai_agent_startup.tools import sandbox


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    """把工具沙箱指向临时目录，审计日志放在沙箱外，默认「用户同意」。"""
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "SANDBOX_DIR", sandbox_dir)
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(sandbox, "confirmer", lambda prompt, timeout: True)
    return sandbox_dir
