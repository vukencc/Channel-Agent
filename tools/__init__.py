"""工具包：导入具体工具模块即完成注册。

新增工具时：写一个 tools/xxx.py，然后在本文件加一行 import。
"""
from tools.base import Tool, TOOL_REGISTRY, register_tool

# 导入即注册
from tools import command, file_crud, rag_search, web_search  # noqa: F401  (仅为了触发注册)

import config
if config.ENABLE_AGENT_TASKS:
    from tools.agent_tasks import register_agent_tasks
    register_agent_tasks()
if config.ENABLE_COMMAND_JOBS:
    from tools.command_jobs import register_command_jobs
    register_command_jobs()
if config.ENABLE_FILE_EXTRAS:
    from tools.file_extras import register_file_extras
    register_file_extras()
if config.ENABLE_DEBUG_TOOL:
    from tools.debug import ToolDebugArgs, tool_debug
    register_tool(ToolDebugArgs, name='tool_debug')(tool_debug)

__all__ = ["Tool", "TOOL_REGISTRY", "register_tool", "all_schemas"]


def all_schemas() -> list[dict]:
    """把注册表里的工具编译成模型需要的清单。"""
    return [tool.schema for tool in TOOL_REGISTRY.values()]
