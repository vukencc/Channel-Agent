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


active_profile = ContextVar('active_model_profile', default=None)


@contextmanager
def model_profile(name):
    token = active_profile.set(name)
    try:
        yield
    finally:
        active_profile.reset(token)
