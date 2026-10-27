"""工具包：导入具体工具模块即完成注册。

新增工具时：写一个 tools/xxx.py，然后在本文件加一行 import。
"""
from agent_core.tools.base import Tool, TOOL_REGISTRY, register_tool

# 导入即注册
from agent_core.tools import command, file_crud, rag_search, web_search, session_tools, permission_request  # noqa: F401  (仅为了触发注册)

from agent_core import config
from agent_core.tools import environment  # noqa: F401
if config.ENABLE_STRUCTURED_CONTEXT:
    from agent_core.tools import context_history  # noqa: F401
if config.ENABLE_TASK_PLANS:
    from agent_core.tools.task_plans import register_task_plans
    register_task_plans()
if config.ENABLE_AGENT_TASKS:
    from agent_core.tools.agent_tasks import register_agent_tasks
    register_agent_tasks()
if config.ENABLE_COMMAND_JOBS:
    from agent_core.tools.command_jobs import register_command_jobs
    register_command_jobs()
if config.ENABLE_FILE_EXTRAS:
    from agent_core.tools.file_extras import register_file_extras
    register_file_extras()
__all__ = ["Tool", "TOOL_REGISTRY", "register_tool", "all_schemas"]


def all_schemas() -> list[dict]:
    """完整执行注册表 schema；实际模型目录另经会话允许列表与别名策略处理。"""
    return [tool.schema for tool in TOOL_REGISTRY.values()]
