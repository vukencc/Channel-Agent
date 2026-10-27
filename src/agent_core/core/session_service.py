"""可信界面和模型工具共用的会话入口；调用者身份不进入工具参数。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import BaseModel, Field

from agent_core.core.prompts import DEFAULT_PROMPT


@dataclass(frozen=True)
class SessionServiceContext:
    manager: object
    caller_id: str | None
    loop: object | None = None
    prompt: str = DEFAULT_PROMPT
    workspace_path: str | None = None
    execution_environment: str | None = None


_service: ContextVar[SessionServiceContext | None] = ContextVar('session_service', default=None)


class RootSessionArgs(BaseModel):
    """可信界面的根会话输入校验，与模型工具注册无关。"""
    title: str = Field(default='新会话', min_length=1, max_length=120)


@contextmanager
def session_service_context(manager, caller_id, loop=None, *, prompt=DEFAULT_PROMPT, workspace_path=None, execution_environment=None):
    if caller_id is not None and (workspace_path is not None or execution_environment is not None):
        raise PermissionError('模型调用不能指定宿主工作区或执行环境')
    token = _service.set(SessionServiceContext(manager, caller_id, loop, prompt, workspace_path, execution_environment))
    try:
        yield
    finally:
        _service.reset(token)


def current_service():
    context = _service.get()
    if context is None:
        raise PermissionError('会话工具只能在会话管理器内执行')
    return context


def has_session_service() -> bool:
    """只区分宿主未注入服务与已注入服务；权限拒绝不能触发兼容回退。"""
    return _service.get() is not None


def open_session(manager, title='新会话', prompt=DEFAULT_PROMPT, *, workspace_path=None, execution_environment=None):
    """可信用户直接创建根会话；工作区、环境仍由管理器验证。"""
    from agent_core.tools.sandbox import ToolContext, audit, tool_context
    arguments = RootSessionArgs(title=title)
    session = manager.create(arguments.title, prompt, workspace_path=workspace_path,
                             execution_environment=execution_environment)
    context = ToolContext(manager.workspace(session), manager.store.directory(session.id) / 'audit.jsonl',
                          lambda *_: False, session.cancelled, session_id=session.id,
                          root_identity=manager.workspaces.identity(session),
                          execution_environment=manager.execution_environment(session))
    with tool_context(context):
        audit('session_created', source='user')
    return session


def cancel_owned_agent_task(context: SessionServiceContext, task_id: str) -> str:
    """在管理器事件循环内取消调用者自己的任务，包括按需初始化的队列。"""
    from agent_core.tools.sandbox import audit, cancellation_requested
    manager = context.manager
    if context.caller_id is None or context.caller_id not in manager.sessions:
        raise PermissionError('取消任务需要运行中的调用者 Agent')
    if cancellation_requested() or manager.closing:
        raise ValueError('会话操作已取消')
    queue = manager.agent_tasks
    try:
        if queue is None:
            raise PermissionError('任务不存在或不属于当前会话')
        queue.cancel(context.caller_id, task_id)
    except PermissionError:
        audit('agent_task_cancel_denied', task_id=task_id)
        raise
    audit('agent_task_cancel_requested', task_id=task_id)
    return '已请求取消；请查询最终状态。'
