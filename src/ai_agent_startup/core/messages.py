"""消息与会话状态的基本定义。"""
from typing import TypedDict


class AgentState(TypedDict):
    prompt: str


def PackMessage(role: str, content: str):
    return {"role": role, "content": content}
