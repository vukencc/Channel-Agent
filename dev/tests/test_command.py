"""命令执行工具测试：正常执行、沙箱工作目录、危险命令拦截、超时、输出截断、确认。"""
import config
from tools import sandbox
from tools.command import run_command


# ------------------------------------------------------------------
# 正常执行
# ------------------------------------------------------------------

def test_run_simple_command(sandbox_env):
    result = run_command("ls -la")
    assert "[退出码] 0" in result


def test_command_runs_in_sandbox_cwd(sandbox_env):
    (sandbox_env / "marker.txt").write_text("x", encoding="utf-8")

    result = run_command("ls")
    assert "marker.txt" in result


def test_nonzero_exit_code_reported(sandbox_env):
    result = run_command("ls /no-such-path-xyz")
    assert "[退出码] 0" not in result
    assert "[stderr]" in result


def test_shell_metacharacters_are_not_interpreted(sandbox_env):
    """shell=False：管道/重定向交给程序当普通参数，不会被执行。"""
    result = run_command("echo a; echo b")
    assert "[退出码] 0" in result
    assert "a; echo b" in result


# ------------------------------------------------------------------
# 危险命令拦截
# ------------------------------------------------------------------

def test_dangerous_commands_blocked(sandbox_env):
    for cmd in ["rm -rf /", "sudo ls", "sh -c whoami", "python3 -c pass", "curl http://x"]:
        result = run_command(cmd)
        assert "[已拦截]" in result, cmd


def test_absolute_program_path_blocked(sandbox_env):
    assert "[已拦截]" in run_command("/bin/ls")


def test_blocked_command_skips_confirmation(sandbox_env, monkeypatch):
    asked = []
    monkeypatch.setattr(sandbox, "confirmer", lambda p, t: asked.append(p) or True)

    run_command("rm -rf /")
    assert asked == []          # 危险命令直接拦，不进入确认环节


# ------------------------------------------------------------------
# 确认
# ------------------------------------------------------------------

def test_command_denied_by_user(sandbox_env, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "confirmer", lambda prompt, timeout: False)

    result = run_command("ls")
    assert "[已取消]" in result


def test_confirm_timeout_aborts_command(sandbox_env, monkeypatch):
    monkeypatch.setattr(sandbox, "confirmer", None)
    monkeypatch.setattr(sandbox.select, "select", lambda r, w, x, t: ([], [], []))

    assert "[已取消]" in run_command("ls")


# ------------------------------------------------------------------
# 超时与输出截断
# ------------------------------------------------------------------

def test_command_timeout(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, "COMMAND_TIMEOUT", 0.3)

    result = run_command("sleep 5")
    assert "[超时]" in result


def test_output_truncated(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, "TOOL_MAX_OUTPUT", 20)

    result = run_command("echo " + "a" * 200)
    assert "已截断" in result
    assert result.count("a") < 200


# ------------------------------------------------------------------
# 审计
# ------------------------------------------------------------------

def test_audit_records_block_and_execute(sandbox_env):
    run_command("rm -rf /")
    run_command("ls")

    log = config.AUDIT_LOG.read_text(encoding="utf-8")
    assert '"event": "blocked"' in log
    assert '"event": "confirm"' in log
    assert '"event": "executed"' in log
