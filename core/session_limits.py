"""会话局部预算；不修改进程级安全配置。"""
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict, Field

import config


class SessionLimits(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    MAX_TOOL_ROUNDS: int | None = Field(default=None, ge=1, le=256)
    MAX_TOOL_CALLS_PER_ROUND: int | None = Field(default=None, ge=1, le=32)
    MODEL_INPUT_CHARS: int | None = Field(default=None, ge=512, le=1048576)
    MODEL_INPUT_TOKENS: int | None = Field(default=None, ge=128, le=262144)
    MODEL_OUTPUT_CHARS: int | None = Field(default=None, ge=1, le=1048576)
    TOOL_TIMEOUT: float | None = Field(default=None, gt=0, le=3600)
    MODEL_RECOVERY_LIMIT: int | None = Field(default=None, ge=0, le=3)


_active = ContextVar('session_limits', default=None)


def validate_limits(values):
    return SessionLimits.model_validate(values).model_dump(exclude_none=True)


def effective_limits(record):
    overrides = validate_limits(record.get('budget_overrides', {})) if config.ENABLE_SESSION_BUDGETS else {}
    return {name: overrides.get(name, getattr(config, name)) for name in SessionLimits.model_fields}


def preset(name):
    defaults = effective_limits({})
    if name == 'standard':
        return defaults
    if name not in {'conservative', 'aggressive'}:
        raise ValueError('预算预设为 conservative/standard/aggressive')
    factor = .5 if name == 'conservative' else 2
    # 预设只调轮次和输出，不隐式扩大工具超时或安全权限。
    defaults['MAX_TOOL_ROUNDS'] = min(256, max(1, int(defaults['MAX_TOOL_ROUNDS'] * factor)))
    defaults['MODEL_OUTPUT_CHARS'] = min(1048576, max(1, int(defaults['MODEL_OUTPUT_CHARS'] * factor)))
    return validate_limits(defaults)


@contextmanager
def limits_scope(record):
    token = _active.set(effective_limits(record))
    try:
        yield
    finally:
        _active.reset(token)


def limit(name):
    values = _active.get()
    return values[name] if values is not None else getattr(config, name)
