"""显式委派、查询和取消；结果通过调用者自己的 tool_call_id 返回。"""
import asyncio
import json

from pydantic import BaseModel, Field

from ai_agent_startup.tools.base import register_tool
from ai_agent_startup.tools.sandbox import _context, ask_permission, audit, truncate


class DelegateArgs(BaseModel):
    """确认后启动独立子代理；先声明任务、工具子集与轮次，用 task_status 查询结果。"""
    task: str = Field(min_length=1, max_length=16000)
    tool_names: list[str]
    max_rounds: int = Field(default=6, ge=1, le=256)
    delay_seconds: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)


class AgentTaskArgs(BaseModel):
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')


def bridge(operation):
    context = _context.get()
    if context is None or context.agent_tasks is None or context.event_loop is None:
        raise PermissionError('当前会话未启用子代理')
    async def run():
        result = operation(context.agent_tasks, context.root.name)
        if asyncio.iscoroutine(result):
            return await result
        return result
    # 管理器控制超时和取消；本桥只做很短的文件提交，不等待子代理推理。
    return asyncio.run_coroutine_threadsafe(run(), context.event_loop).result()


def delegate(task, tool_names, max_rounds=6, delay_seconds=0):
    detail = f'{task}\n工具={tool_names}；轮次={max_rounds}；延时={delay_seconds:g}s'
    if not ask_permission('delegate', detail, force_confirmation=True):
        return '[已取消] 用户未确认委派。'
    identifier = bridge(lambda queue, owner: queue.start(queue.manager.sessions[owner], task, tool_names, max_rounds, delay_seconds))
    audit('agent_task_queued', task_id=identifier, tool_names=tool_names, max_rounds=max_rounds)
    return identifier


def task_status(task_id):
    return truncate(json.dumps(bridge(lambda queue, owner: queue.status(owner, task_id)), ensure_ascii=False))


def cancel_agent_task(task_id):
    bridge(lambda queue, owner: queue.cancel(owner, task_id))
    audit('agent_task_cancel_requested', task_id=task_id)
    return '已请求取消；请查询最终状态。'


def register_agent_tasks():
    register_tool(DelegateArgs, concurrency='control')(delegate)
    register_tool(AgentTaskArgs, concurrency='read')(task_status)
    register_tool(AgentTaskArgs, concurrency='control')(cancel_agent_task)
