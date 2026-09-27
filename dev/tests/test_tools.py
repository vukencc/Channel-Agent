"""工具注册表与 Tool.run 的参数校验测试（不需要网络）。"""
import pytest
from pydantic import ValidationError

from tools import TOOL_REGISTRY, all_schemas


def test_expected_tools_registered():
    assert {"web_search", "tool_debug"} <= set(TOOL_REGISTRY)


def test_all_schemas_match_registry():
    schemas = all_schemas()
    assert len(schemas) == len(TOOL_REGISTRY)
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["name"] in TOOL_REGISTRY


def test_tool_debug_respects_max_and_returns_str():
    tool = TOOL_REGISTRY["tool_debug"]
    for _ in range(20):
        result = tool.run('{"max": 100}')
        assert isinstance(result, str)
        assert 0.0 <= float(result) < 100.0


def test_tool_run_uses_defaults_for_empty_arguments():
    tool = TOOL_REGISTRY["tool_debug"]
    assert 0.0 <= float(tool.run("")) < 1.0  # 空参数 → 使用默认 max=1


def test_tool_run_rejects_missing_required_argument():
    # web_search 的 query 是必填；这正是之前 tool_debug 被误判时踩的坑
    tool = TOOL_REGISTRY["web_search"]
    with pytest.raises(ValidationError):
        tool.run('{"max_results": 3}')


def test_tool_run_rejects_invalid_json():
    tool = TOOL_REGISTRY["tool_debug"]
    with pytest.raises(ValidationError):
        tool.run("not-json")


def test_unknown_tool_is_absent():
    assert TOOL_REGISTRY.get("no_such_tool") is None
