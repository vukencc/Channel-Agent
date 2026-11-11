"""打包自检覆盖动态工具定义和便携模板。"""
from agent_test.scripts import packaged_smoke


def test_dynamic_tool_capability_round_trip(tmp_path):
    checks = packaged_smoke._capabilities()
    assert "dynamic_tools" in checks
    checks["dynamic_tools"](tmp_path)


def test_dynamic_tool_capability_requires_json_schema_resources(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator

    def unavailable(_schema):
        raise RuntimeError("missing schema resources")

    monkeypatch.setattr(Draft202012Validator, "check_schema", unavailable)
    from agent_core.core.custom_tools import _check_schema
    _check_schema.cache_clear()
    try:
        try:
            packaged_smoke._capabilities()["dynamic_tools"](tmp_path)
        except RuntimeError as exc:
            assert str(exc) == "missing schema resources"
        else:
            raise AssertionError("schema 资源故障未被发现")
    finally:
        _check_schema.cache_clear()
