"""后台命令控制工具；任务 ID 始终绑定当前会话。"""
import json

from pydantic import BaseModel, Field

from tools.base import register_tool
from tools.sandbox import _context, truncate


class StartCommandJobArgs(BaseModel):
    """确认后后台执行隔离命令；返回任务 ID，使用 job_status/job_logs 检查真实结果。"""
    command: str
    reason: str = ''
    network: bool = False
    timeout: float | None = Field(default=None, gt=0)


class CommandJobArgs(BaseModel):
    job_id: str = Field(pattern=r'^[a-f0-9]{32}$')


def current_jobs():
    context = _context.get()
    if context is None or context.job_manager is None:
        raise PermissionError('当前会话未启用后台命令')
    return context.job_manager, context.root.name


def start_command_job(command, reason='', network=False, timeout=None):
    jobs, _ = current_jobs()
    return jobs.start(command, reason, network, timeout)


def job_status(job_id):
    jobs, owner = current_jobs()
    return truncate(json.dumps(jobs.status(owner, job_id), ensure_ascii=False))


def job_logs(job_id):
    jobs, owner = current_jobs()
    return truncate(jobs.logs(owner, job_id))


def cancel_command_job(job_id):
    jobs, owner = current_jobs()
    jobs.cancel(owner, job_id)
    return '已请求取消；用 job_status 确認最终状态。'


def register_command_jobs():
    register_tool(StartCommandJobArgs, concurrency='control')(start_command_job)
    register_tool(CommandJobArgs, concurrency='read')(job_status)
    register_tool(CommandJobArgs, concurrency='read')(job_logs)
    register_tool(CommandJobArgs, concurrency='control')(cancel_command_job)
