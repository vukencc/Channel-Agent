"""会话创建与有调用关系的 Agent 间通信。"""
import asyncio
import json
from functools import partial

from pydantic import BaseModel, ConfigDict, Field

from ai_agent_startup import config
from ai_agent_startup.core.session_service import current_service
from ai_agent_startup.tools.base import TOOL_REGISTRY, register_tool
from ai_agent_startup.tools.sandbox import ToolContext, ask_permission, audit, cancellation_requested, tool_context


class CreateSessionArgs(BaseModel):
    """任务不适合单 Agent 执行、可拆成独立任务时调用。确认后创建独立上下文和工作区的子 Agent；返回 session_id/task_id。用 send_session_message 与子 Agent 协作；子 Agent 不得递归创建。"""
    model_config = ConfigDict(extra='forbid')
    title: str = Field(default='新会话', min_length=1, max_length=120)
    task: str = Field(default='', max_length=16000, description='子 Agent 的具体任务；模型调用时不能为空')
    tool_names: list[str] | None = Field(default=None, description='父工具集的子集；省略则继承可用工具，排除再次委派/长命令')
    max_rounds: int = Field(default=6, ge=1, le=256)


class SendSessionMessageArgs(BaseModel):
    """向直接父/子 Agent 的持久信箱发送信息。收件方在下一次模型轮次读取；不会自动启动空闲 Agent。"""
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    message: str = Field(min_length=1, max_length=config.SESSION_MESSAGE_MAX_CHARS)


class ReadSessionMessagesArgs(BaseModel):
    """分页读取自己的 Agent 信箱。使用返回的 next_seq 读取下一页；不会删除消息或自动运行任务。"""
    model_config = ConfigDict(extra='forbid')
    after_seq: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


def _bridge(operation):
    context = current_service()
    if context.caller_id is None or context.loop is None:
        raise PermissionError('此操作需要运行中的调用者 Agent')
    async def run():
        if cancellation_requested() or context.manager.closing:
            raise ValueError('会话操作已取消')
        return await operation(context)
    return asyncio.run_coroutine_threadsafe(run(), context.loop).result()


@register_tool(CreateSessionArgs, concurrency='control')
def create_session(title='新会话', task='', tool_names=None, max_rounds=6):
    context = current_service()
    manager = context.manager
    if context.caller_id is None:
        if task or tool_names is not None:
            raise ValueError('界面入口仅创建空会话')
        session = manager.create(title, context.prompt)
        audit_context = ToolContext(manager.store.workspace_path(session.id),
            manager.store.directory(session.id) / 'audit.jsonl', lambda *_: False, session.cancelled)
        with tool_context(audit_context):
            audit('session_created', source='user')
        return json.dumps({'session_id': session.id, 'task_id': None})
    parent = manager.sessions[context.caller_id]
    if not task.strip():
        raise ValueError('Agent 创建子会话必须给出非空 task')
    if parent.record.get('delegated_from'):
        raise PermissionError('子 Agent 不得递归创建会话')
    parent_tools = parent.record.get('tool_names', config.MODEL_TOOL_NAMES)
    allowed = set(TOOL_REGISTRY if parent_tools is None else parent_tools) - {'create_session', 'delegate', 'start_command_job'}
    names = sorted(allowed) if tool_names is None else tool_names
    if not set(names) <= allowed:
        raise PermissionError('子 Agent 工具必须为父会话工具子集，不得再次委派')
    if not ask_permission('create_session', f'{title}：{task}\n工具={names}；轮次={max_rounds}', force_confirmation=True):
        return '[已取消] 用户未确认创建子 Agent。'
    async def start(context):
        from ai_agent_startup.core.agent_tasks import AgentTasks
        async with manager.agent_tasks_init_lock:
            if manager.agent_tasks is None:
                manager.agent_tasks = await context.loop.run_in_executor(manager.store.writer,
                    partial(AgentTasks, manager, require_budget_config=False))
        if cancellation_requested() or manager.closing:
            raise ValueError('创建子 Agent 已取消')
        identifier = await manager.agent_tasks.start(parent, task, names, max_rounds, title=title)
        child_id = manager.agent_tasks.records[identifier]['child_id']
        audit('session_created', child_id=child_id, task_id=identifier, source='agent')
        return json.dumps({'session_id': child_id, 'task_id': identifier})
    return _bridge(start)


@register_tool(SendSessionMessageArgs, concurrency='control')
def send_session_message(session_id, message):
    context = current_service()
    if context.caller_id is None or context.loop is None or cancellation_requested():
        raise PermissionError('发送消息需要未取消的调用者 Agent')
    # 已在工具线程，直接执行原子写；嵌套 to_thread 会在并发饱和时死锁。
    result = context.manager.communication.send(context.caller_id, session_id, message)
    context.loop.call_soon_threadsafe(context.manager.notify)
    return json.dumps(result, ensure_ascii=False)


@register_tool(ReadSessionMessagesArgs, concurrency='read')
def read_session_messages(after_seq=0, limit=20):
    context = current_service()
    if context.caller_id is None:
        raise PermissionError('读取信箱需要调用者 Agent')
    result = context.manager.communication.read(context.caller_id, after_seq, limit,
                                                max_chars=config.TOOL_MAX_OUTPUT)
    audit('session_messages_read', count=len(result['messages']), after_seq=after_seq)
    return json.dumps(result, ensure_ascii=False)
