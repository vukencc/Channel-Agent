"""可信界面和模型工具共用的会话入口；调用者身份不进入工具参数。"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ai_agent_startup.core.prompts import DEFAULT_PROMPT


@dataclass(frozen=True)
class SessionServiceContext:
    manager: object
    caller_id: str | None
    loop: object | None = None
    prompt: str = DEFAULT_PROMPT


_service: ContextVar[SessionServiceContext | None] = ContextVar('session_service', default=None)


@contextmanager
def session_service_context(manager, caller_id, loop=None, *, prompt=DEFAULT_PROMPT):
    token = _service.set(SessionServiceContext(manager, caller_id, loop, prompt))
    try:
        yield
    finally:
        _service.reset(token)


def current_service():
    context = _service.get()
    if context is None:
        raise PermissionError('会话工具只能在会话管理器内执行')
    return context


def open_session(manager, title='新会话', prompt=DEFAULT_PROMPT):
    """用户显式新建：调用注册工具一次，不额外请求模型。"""
    from ai_agent_startup.tools import TOOL_REGISTRY
    with session_service_context(manager, None, prompt=prompt):
        result = TOOL_REGISTRY['create_session'].run(json.dumps({'title': title}, ensure_ascii=False))
    return manager.sessions[json.loads(result)['session_id']]
