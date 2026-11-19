"""pytest 公共夹具。"""
import os

import pytest

# 测试默认行为不受用户忽略的 .env 污染；显式启用功能的用例自行 monkeypatch。
# 必须在首次导入 config/tools 前设置，子进程也继承相同基线。
for name in ('AUTO_CONTINUE_TASKS', 'ENABLE_STRUCTURED_CONTEXT', 'ENABLE_TASK_PLANS',
             'ENABLE_ENVIRONMENT_PREPARE', 'WORKSPACE_MEMORY_ENABLED'):
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

from agent_core import config
from agent_core.tools import sandbox


@pytest.fixture(autouse=True)
def isolated_default_audit(tmp_path, monkeypatch):
    """未设置工具上下文的测试也不能污染用户主审计；子进程沿用隔离路径。"""
    path = tmp_path / 'default-audit.jsonl'
    monkeypatch.setattr(config, 'AUDIT_LOG', path)
    monkeypatch.setenv('AUDIT_LOG', str(path))


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    """隔离工具文件与审计；默认 standard 验证确认路径，其他权限用例显式选择策略。"""
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "SANDBOX_DIR", sandbox_dir)
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "standard")
    monkeypatch.setattr(sandbox, "confirmer", lambda prompt, timeout: True)
    return sandbox_dir


@pytest.fixture
def symlink_capable(tmp_path):
    """只在宿主明确缺少创建符号链接权限时跳过相关安全用例。"""
    target = tmp_path / 'symlink-capability-target'
    target.write_text('probe', encoding='utf-8')
    link = tmp_path / 'symlink-capability-link'
    try:
        link.symlink_to(target)
    except OSError as exc:
        if os.name == 'nt' and exc.winerror == 1314:
            pytest.skip('Windows requires Developer Mode or symlink privilege (WinError 1314)')
        raise
    link.unlink()


@pytest.fixture
def headless_cli_session():
    """patch_stdout 与 Application 共用受控输出，不请求 Windows 控制台。"""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.output import DummyOutput

    with create_app_session(output=DummyOutput()):
        yield


@pytest.fixture
def lookup_command_transport(monkeypatch, request):
    """仅两个固定读取用例：Linux/显式 WSL 真实执行；离线 Windows 用真实子进程。

    此测试传输不是 Windows 沙箱验证，不发布通用执行能力，也不伪造读取结果。
    RUN_WINDOWS_WSL_TESTS=1 保留真实桥；不把离线传输记为 WSL 沙箱通过。
    """
    import base64
    import json
    from pathlib import Path
    import subprocess
    import sys

    allowed_tests = {
        "test_real_custom_read_result_is_lower_trust",
        "test_selected_dynamic_report_and_node_task_reach_independent_review",
    }
    assert request.node.originalname in allowed_tests
    allowed_code = {
        'import json\nfrom pathlib import Path\ndef main(arguments):\n    return json.loads(Path(arguments["path"]).read_text(encoding="utf-8"))\n',
        "def main(arguments):\n    import json\n    from pathlib import Path\n    rows = json.loads(Path(arguments['path']).read_text(encoding='utf-8'))\n    return [row for row in rows if row['id'] in arguments['ids']]\n",
    }
    native_fixture = os.name == "nt" and os.environ.get("RUN_WINDOWS_WSL_TESTS") != "1"
    label = ("test_native_python_subprocess" if native_fixture else
             "real_windows_wsl" if os.name == "nt" else "real_linux_bubblewrap")
    request.node.user_properties.append(("lookup_transport", label))

    def install(definition, workspace):
        assert definition["name"] == "lookup_feedback" and definition["code"] in allowed_code
        root = Path(workspace).resolve(strict=True)
        assert root.is_relative_to(request.getfixturevalue("tmp_path").resolve(strict=True))
        if not native_fixture:
            return label
        from agent_core.core.command_review import bound_review
        from agent_core.tools import wsl_command

        def execute_lookup(operation, arguments):
            assert operation == "run_command"
            command = arguments["command"]
            review = bound_review(command, arguments.get("network", False))
            assert (review is not None and review.name == "lookup_feedback"
                    and review.code == definition["code"] and not review.network)
            assert arguments.get("runtime_profile", "system") == "system"
            assert arguments.get("resource_profile", "standard") == "standard"
            values = json.loads(review.arguments_json)
            assert set(values) in ({"path"}, {"path", "ids"})
            assert values["path"] == "feedback.json"
            if "ids" in values:
                assert values["ids"] == ["CF-01"]
            assert sandbox.sandbox_root().resolve(strict=True) == root
            assert (root / "feedback.json").resolve(strict=True).parent == root
            assert not sandbox.cancellation_requested()
            if not sandbox.ask_permission("run_command", command, arguments.get("reason", ""), command=command):
                return "[已取消] 用户未确认（拒绝或确认超时），命令未执行。"
            # 固定子进程包装只接收经过上方逐字白名单绑定的两段只读源码。
            script = ("import base64,json,sys; ns={}; "
                      "exec(compile(base64.b64decode(sys.argv[1]),'<test-lookup>','exec'),ns); "
                      "print(json.dumps(ns['main'](json.loads(sys.argv[2])),ensure_ascii=False,allow_nan=False))")
            code = base64.b64encode(review.code.encode("utf-8")).decode("ascii")
            env = {name: os.environ[name] for name in ("SystemRoot", "TEMP", "TMP") if name in os.environ}
            with sandbox.execution_authority():
                sandbox.audit("command_started", strict=True, test_transport=label)
                process = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", script,
                                          code, review.arguments_json], cwd=root, env=env,
                                         stdin=subprocess.DEVNULL, capture_output=True,
                                         encoding="utf-8", timeout=config.COMMAND_TIMEOUT,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                sandbox.audit("command", strict=True, exit_code=process.returncode, test_transport=label)
            # 与命令工具相同的协议封装，内容和退出码全部来自真实进程。
            parts = [f"[退出码] {process.returncode}",
                     f"[stdout]\n{sandbox.truncate(process.stdout)}" if process.stdout else "[stdout] （空）"]
            if process.stderr:
                parts.append(f"[stderr]\n{sandbox.truncate(process.stderr)}")
            return "\n".join(parts)

        monkeypatch.setattr(wsl_command, "execute_wsl_tool", execute_lookup)
        return label

    return install
