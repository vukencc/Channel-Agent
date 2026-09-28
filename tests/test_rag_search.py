"""rag/tool.py 语义参数映射测试（纯函数，不需要模型或网络）。"""
import pytest

from ai_agent_startup.rag.tool import resolve_params


def test_strictness_maps_to_increasing_threshold():
    strict, _ = resolve_params(strictness="strict")
    normal, _ = resolve_params(strictness="normal")
    loose, _ = resolve_params(strictness="loose")
    assert strict > normal > loose


def test_breadth_maps_to_increasing_top_k():
    _, narrow = resolve_params(breadth="narrow")
    _, normal = resolve_params(breadth="normal")
    _, wide = resolve_params(breadth="wide")
    assert narrow < normal < wide


def test_defaults_are_normal():
    assert resolve_params() == resolve_params(strictness="normal", breadth="normal")


def test_invalid_values_raise():
    with pytest.raises(ValueError):
        resolve_params(strictness="ultra")
    with pytest.raises(ValueError):
        resolve_params(breadth="massive")
