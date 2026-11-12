"""可验证的模型采样参数和上下文隔离的命名配置。"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['text', 'json_object']


class ModelParameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, gt=0, strict=True)
    parallel_tool_calls: bool | None = Field(default=None, strict=True)
    response_format: ResponseFormat | None = None
    tool_choice: Literal['auto', 'none', 'required'] | None = None


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str | None = Field(default=None, min_length=1)
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    # 容量属于本地预算元数据，不能作为供应商采样参数发送。
    context_tokens: int | None = Field(default=None, ge=512, le=2097152, strict=True)
    response_reserve_tokens: int | None = Field(default=None, ge=0, le=2097151, strict=True)


active_profile = ContextVar('active_model_profile', default=None)
active_endpoint = ContextVar('active_model_endpoint', default=None)
active_reasoning_effort = ContextVar('active_reasoning_effort', default=None)
active_thinking_mode = ContextVar('active_thinking_mode', default=None)

# 默认继承配置；None 表示当前请求不设置整体时限。
DEFAULT_MODEL_DEADLINE = object()
active_model_deadline = ContextVar('active_model_deadline', default=DEFAULT_MODEL_DEADLINE)


@contextmanager
def model_deadline(timeout: float | None):
    token = active_model_deadline.set(timeout)
    try:
        yield
    finally:
        active_model_deadline.reset(token)


@contextmanager
def model_thinking(reasoning_effort=None, thinking_mode=None):
    effort_token = active_reasoning_effort.set(reasoning_effort)
    mode_token = active_thinking_mode.set(thinking_mode)
    try:
        yield
    finally:
        active_thinking_mode.reset(mode_token)
        active_reasoning_effort.reset(effort_token)


@contextmanager
def model_profile(name, endpoint=None):
    token = active_profile.set(name)
    endpoint_token = active_endpoint.set(endpoint)
    try:
        yield
    finally:
        active_endpoint.reset(endpoint_token)
        active_profile.reset(token)
