"""文件 CRUD 工具测试：正常流程、路径逃逸、确认通过/拒绝/超时、审计。"""
import config
from tools import sandbox
from tools.file_crud import create_file, delete_file, list_files, read_file, update_file


# ------------------------------------------------------------------
# 正常流程
# ------------------------------------------------------------------

def test_full_crud_flow(sandbox_env):
    assert "[完成]" in create_file("notes/a.txt", "hello", "记录备忘")
    assert (sandbox_env / "notes/a.txt").read_text(encoding="utf-8") == "hello"

    assert read_file("notes/a.txt") == "hello"

    listing = list_files("notes")
    assert "notes/a.txt" in listing and "[file]" in listing

    assert "[完成]" in update_file("notes/a.txt", "world")
    assert read_file("notes/a.txt") == "world"

    assert "[完成]" in delete_file("notes/a.txt")
    assert not (sandbox_env / "notes/a.txt").exists()


def test_create_refuses_to_overwrite(sandbox_env):
    create_file("a.txt", "v1")
    assert "[失败]" in create_file("a.txt", "v2")
    assert read_file("a.txt") == "v1"


def test_update_requires_existing_file(sandbox_env):
    assert "[失败]" in update_file("missing.txt", "x")


def test_read_missing_file(sandbox_env):
    assert "[失败]" in read_file("nope.txt")


def test_list_missing_dir(sandbox_env):
    assert "[失败]" in list_files("nope")


def test_empty_dir_listing(sandbox_env):
    assert "（空）" in list_files(".")


def test_list_does_not_follow_symlinks(sandbox_env):
    outside = sandbox_env.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (sandbox_env / "link.txt").symlink_to(outside)

    listing = list_files(".")
    assert "[link] link.txt" in listing


# ------------------------------------------------------------------
# 路径逃逸
# ------------------------------------------------------------------

def test_absolute_path_rejected(sandbox_env):
    assert "[已拦截]" in read_file("/etc/passwd")
    assert "[已拦截]" in create_file("/tmp/evil.txt", "x")


def test_parent_traversal_rejected(sandbox_env):
    (sandbox_env.parent / "secret.txt").write_text("secret", encoding="utf-8")

    assert "[已拦截]" in read_file("../secret.txt")
    assert "[已拦截]" in create_file("../evil.txt", "x")
    assert not (sandbox_env.parent / "evil.txt").exists()


def test_symlink_escape_rejected(sandbox_env):
    outside = sandbox_env.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (sandbox_env / "link.txt").symlink_to(outside)

    assert "[已拦截]" in read_file("link.txt")


def test_symlinked_dir_escape_rejected(sandbox_env):
    outside_dir = sandbox_env.parent / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "f.txt").write_text("x", encoding="utf-8")
    (sandbox_env / "linkdir").symlink_to(outside_dir, target_is_directory=True)

    assert "[已拦截]" in read_file("linkdir/f.txt")


# ------------------------------------------------------------------
# 确认：通过 / 拒绝 / 超时
# ------------------------------------------------------------------

def test_read_does_not_require_confirmation(sandbox_env, monkeypatch):
    create_file("a.txt", "x")

    asked = []
    monkeypatch.setattr(sandbox, "confirmer", lambda p, t: asked.append(p) or False)

    assert read_file("a.txt") == "x"
    assert asked == []          # 读操作不应触发确认


def test_create_denied_by_user(sandbox_env, monkeypatch):
    monkeypatch.setattr(sandbox, "confirmer", lambda prompt, timeout: False)

    assert "[已取消]" in create_file("a.txt", "x")
    assert not (sandbox_env / "a.txt").exists()


def test_delete_denied_by_user_keeps_file(sandbox_env, monkeypatch):
    create_file("a.txt", "x")
    monkeypatch.setattr(sandbox, "confirmer", lambda prompt, timeout: False)

    assert "[已取消]" in delete_file("a.txt")
    assert (sandbox_env / "a.txt").exists()


def test_confirm_timeout_aborts(sandbox_env, monkeypatch):
    """走真实终端确认实现：select 超时 → 视为拒绝 → 不落盘。"""
    monkeypatch.setattr(sandbox, "confirmer", None)
    monkeypatch.setattr(sandbox.select, "select", lambda r, w, x, t: ([], [], []))

    assert "[已取消]" in create_file("a.txt", "x")
    assert not (sandbox_env / "a.txt").exists()


def test_confirmer_exception_is_fail_closed(sandbox_env, monkeypatch):
    def boom(prompt, timeout):
        raise RuntimeError("UI 挂了")

    monkeypatch.setattr(sandbox, "confirmer", boom)

    assert "[已取消]" in create_file("a.txt", "x")
    assert not (sandbox_env / "a.txt").exists()


# ------------------------------------------------------------------
# 审计
# ------------------------------------------------------------------

def test_audit_records_confirm_and_execute(sandbox_env):
    create_file("a.txt", "x", reason="测试用途")

    log = config.AUDIT_LOG.read_text(encoding="utf-8")
    assert '"event": "confirm"' in log
    assert '"event": "executed"' in log
    assert '"action": "create_file"' in log
    assert '"allowed": true' in log
