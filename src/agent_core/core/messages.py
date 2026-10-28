"""消息与会话状态的基本定义。"""
from typing import TypedDict


class AgentState(TypedDict):
    prompt: str


def PackMessage(role: str, content: str):
    return {"role": role, "content": content}


def is_user_request(message: dict) -> bool:
    """真人请求开启新轮次；Agent 信件是当前任务的参考数据。"""
    return (message.get('role') == 'user' and '_agent_message' not in message
            and not message.get('_context_reference'))
