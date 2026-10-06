"""Agent 主动识别内容风险后的单次计划申请，不改变权限档位。"""
import json

from pydantic import BaseModel, ConfigDict, Field

from ai_agent_startup.tools.base import register_tool
from ai_agent_startup.tools.sandbox import ask_permission, current_policy


class PermissionRequestArgs(BaseModel):
    """当 Smart 模式下计划涉及敏感内容等本地规则无法识别的高风险时，先申请权限。拒绝后停止该操作，不换工具绕过。此申请仅确认本次计划，不改变权限档位，不替代具体工具的安全校验和确认。Full Access 自动允许。"""
    model_config = ConfigDict(extra='forbid')
    action: str = Field(min_length=1, max_length=120, description='计划进行的操作名称')
    detail: str = Field(min_length=1, max_length=4000, description='明确操作对象、范围和影响')
    reason: str = Field(min_length=1, max_length=1000, description='Agent 判断高风险的原因')


@register_tool(PermissionRequestArgs, concurrency='control')
def request_permission(action: str, detail: str, reason: str) -> str:
    allowed = ask_permission('request_permission', f'{action}：{detail}', reason, force_confirmation=True)
    return json.dumps({'allowed': allowed, 'action': action, 'policy': current_policy()}, ensure_ascii=False)
