"""config.py 的环境变量解析测试。"""
import config


def test_env_bool_recognizes_true(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on", " True "):
        monkeypatch.setenv("X_FLAG", value)
        assert config.env_bool("X_FLAG") is True, value


def test_env_bool_recognizes_false(monkeypatch):
    # 回归测试：以前 bool("False") == True，是个 bug
    for value in ("0", "false", "FALSE", "no", "off"):
        monkeypatch.setenv("X_FLAG", value)
        assert config.env_bool("X_FLAG") is False, value


def test_env_bool_missing_uses_default(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config.env_bool("X_FLAG") is False
    assert config.env_bool("X_FLAG", default=True) is True


def test_env_float_falls_back_on_missing_or_blank(monkeypatch):
    monkeypatch.setenv("X_NUM", "12.5")
    assert config.env_float("X_NUM", 0.0) == 12.5
    monkeypatch.setenv("X_NUM", "")
    assert config.env_float("X_NUM", 7.0) == 7.0
    monkeypatch.delenv("X_NUM", raising=False)
    assert config.env_float("X_NUM", 7.0) == 7.0


def test_env_int_falls_back_on_missing_or_blank(monkeypatch):
    monkeypatch.setenv("X_INT", "3")
    assert config.env_int("X_INT", 0) == 3
    monkeypatch.setenv("X_INT", "  ")
    assert config.env_int("X_INT", 9) == 9
