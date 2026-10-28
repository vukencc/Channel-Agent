"""会话局部预算；不修改进程级安全配置。"""
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict, Field

from agent_core import config


class SessionLimits(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    MAX_TOOL_ROUNDS: int | None = Field(default=None, ge=0, le=256)
    MAX_TOOL_CALLS_PER_ROUND: int | None = Field(default=None, ge=1, le=32)
    MODEL_INPUT_CHARS: int | None = Field(default=None, ge=512, le=8388608)
    MODEL_INPUT_TOKENS: int | None = Field(default=None, ge=128, le=2097152)
    MODEL_OUTPUT_CHARS: int | None = Field(default=None, ge=1, le=1048576)
    TOOL_TIMEOUT: float | None = Field(default=None, gt=0, le=3600)
    MODEL_RECOVERY_LIMIT: int | None = Field(default=None, ge=0, le=3)


_active = ContextVar('session_limits', default=None)
_context_budget = ContextVar('context_budget', default=None)


def validate_limits(values):
    return SessionLimits.model_validate(values).model_dump(exclude_none=True)


def _resolve(record, endpoint=None):
    """在准备上下文前绑定模型容量；输入和输出共享供应商窗口。"""
    overrides = validate_limits(record.get('budget_overrides', {})) if config.ENABLE_SESSION_BUDGETS or record.get('delegated_from') else {}
    values = {name: overrides.get(name, getattr(config, name)) for name in SessionLimits.model_fields}
    profile_name = record.get('model_profile')
    profile = config.MODEL_PROFILES.get(profile_name, {})
    default_model = endpoint is None and (not profile_name or bool(profile) and (not profile.get('model') or profile.get('model') == config.MODEL))
    capacity = (getattr(endpoint, 'context_tokens', None) if endpoint is not None else profile.get('context_tokens'))
    if capacity is None:
        capacity = config.MODEL_CONTEXT_TOKENS if default_model else 0
    reserve = (getattr(endpoint, 'response_reserve_tokens', None) if endpoint is not None else profile.get('response_reserve_tokens'))
    reserve = config.MODEL_RESPONSE_RESERVE_TOKENS if reserve is None else reserve
    options = endpoint.options() if endpoint is not None else {**config.MODEL_PARAMETERS, **profile.get('parameters', {})}
    reserve = max(reserve, options.get('max_tokens') or 0)
    if capacity and (capacity > 2097152 or capacity < 512 or reserve >= capacity):
        raise ValueError('模型上下文容量必须为 512..2097152，且大于输出预留/max_tokens')
    tokens = values['MODEL_INPUT_TOKENS'] or (capacity - reserve if capacity else 16000)
    if capacity:
        tokens = min(tokens, capacity - reserve)
    elif not default_model:
        tokens = min(tokens, 16000)
    chars = values['MODEL_INPUT_CHARS'] or 4 * tokens
    if capacity:
        chars = min(chars, 4 * tokens)
    values.update(MODEL_INPUT_TOKENS=tokens, MODEL_INPUT_CHARS=chars)
    metadata = {'input_limit_tokens': tokens, 'input_limit_chars': chars,
                'context_capacity_tokens': capacity or None, 'response_reserve_tokens': reserve}
    return values, metadata


def effective_limits(record, endpoint=None):
    return _resolve(record, endpoint)[0]


def context_budget_metadata():
    """公开估算口径，不暴露端点、密钥或会话正文。"""
    return dict(_context_budget.get() or _resolve({})[1])


def preset(name):
    defaults = effective_limits({})
    if name == 'standard':
        return defaults
    if name not in {'conservative', 'aggressive'}:
        raise ValueError('预算预设为 conservative/standard/aggressive')
    factor = .5 if name == 'conservative' else 2
    # 预设只调轮次和输出，不隐式扩大工具超时或安全权限。
    if defaults['MAX_TOOL_ROUNDS']:
        defaults['MAX_TOOL_ROUNDS'] = min(256, max(1, int(defaults['MAX_TOOL_ROUNDS'] * factor)))
    defaults['MODEL_OUTPUT_CHARS'] = min(1048576, max(1, int(defaults['MODEL_OUTPUT_CHARS'] * factor)))
    return validate_limits(defaults)


@contextmanager
def limits_scope(record, endpoint=None):
    values, metadata = _resolve(record, endpoint)
    token = _active.set(values)
    budget_token = _context_budget.set(metadata)
    try:
        yield
    finally:
        _active.reset(token)
        _context_budget.reset(budget_token)


def limit(name):
    values = _active.get()
    return values[name] if values is not None else effective_limits({})[name]
