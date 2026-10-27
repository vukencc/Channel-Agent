"""后台命令控制工具；任务 ID 始终绑定当前会话。"""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.tools.base import register_tool
from agent_core.tools.sandbox import _context, truncate


class StartCommandJobArgs(BaseModel):
    """确认后在会话选定的 sandbox/local 环境后台执行；使用 inspect_command_job 检查状态与已保留日志尾部。"""
    command: str
    reason: str = ''
    network: bool = Field(default=False, description='仅 sandbox 控制白名单联网；local 使用本机网络含 localhost')
    timeout: float | None = Field(default=None, gt=0)
    runtime_profile: Literal['system', 'node'] = Field(
        default='system', description='仅 sandbox：普通命令用 system，已配置 Node 用 node；local 使用本机运行时')


class CommandJobArgs(BaseModel):
    job_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class InspectCommandJobArgs(CommandJobArgs):
    """检查自己的后台命令状态、排空情况、执行结果及已保留日志尾部；返回有界 JSON。日志捕获达到上限时可能缺少后续输出。"""
    model_config = ConfigDict(extra='forbid')


def current_jobs():
    context = _context.get()
    if context is None or context.job_manager is None:
        raise PermissionError('当前会话未启用后台命令')
    return context.job_manager, context.session_id or context.root.name


def start_command_job(command, reason='', network=False, timeout=None, runtime_profile='system'):
    jobs, _ = current_jobs()
    return jobs.start(command, reason, network, timeout, runtime_profile=runtime_profile)


def job_status(job_id):
    jobs, owner = current_jobs()
    return truncate(json.dumps(jobs.status(owner, job_id), ensure_ascii=False))


def job_logs(job_id):
    jobs, owner = current_jobs()
    return truncate(jobs.logs(owner, job_id))


def inspect_command_job(job_id):
    jobs, owner = current_jobs()
    return json.dumps(jobs.inspect(owner, job_id), ensure_ascii=False, separators=(',', ':'))


def cancel_command_job(job_id):
    jobs, owner = current_jobs()
    jobs.cancel(owner, job_id)
    return '已请求取消；用 inspect_command_job 确认最终状态。'


def register_command_jobs():
    register_tool(StartCommandJobArgs, concurrency='control')(start_command_job)
    register_tool(InspectCommandJobArgs, concurrency='read')(inspect_command_job)
    register_tool(CommandJobArgs, concurrency='read')(job_status)
    register_tool(CommandJobArgs, concurrency='read')(job_logs)
    register_tool(CommandJobArgs, concurrency='control')(cancel_command_job)
