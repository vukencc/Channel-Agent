"""工具包：导入具体工具模块即完成注册。

新增工具时：写一个 tools/xxx.py，然后在本文件加一行 import。
"""
from tools.base import Tool, TOOL_REGISTRY, register_tool

# 导入即注册
from tools import command, debug, file_crud, rag_search, web_search  # noqa: F401  (仅为了触发注册)

__all__ = ["Tool", "TOOL_REGISTRY", "register_tool", "all_schemas"]


def all_schemas() -> list[dict]:
    """把注册表里的工具编译成模型需要的清单。"""
    return [tool.schema for tool in TOOL_REGISTRY.values()]
