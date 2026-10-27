import random

from pydantic import BaseModel, Field
from agent_core.tools.base import register_tool

class ToolDebugArgs(BaseModel):
    """
    开发生成测试工具，在指定范围内随机生成一个数字，判断大模型
    是否能够正常调用工具
    """
    max: int = Field(
        default=1, description="随机生成数范围上限"
    )


def tool_debug(max: int = 1) -> float:
    """
    开发生成测试工具，在指定范围内随机生成一个数字，判断大模型
    是否能够正常调用工具
    """
    return random.random() * max


def register_debug_tool() -> None:
    """由宿主组合根显式启用，Core 不依赖测试工具。"""
    register_tool(ToolDebugArgs, name='tool_debug')(tool_debug)
