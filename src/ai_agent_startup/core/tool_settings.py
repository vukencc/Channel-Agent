"""会话工具子集：请求 schema 和执行侧使用同一允许列表。"""
from contextlib import contextmanager
from contextvars import ContextVar

from ai_agent_startup import config


active_tool_names = ContextVar('active_tool_names', default='default')


def filter_schemas(available: list[dict], names: list[str] | None) -> list[dict]:
    if names is None:
        return available
    known = {row['function']['name'] for row in available}
    if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) or name not in known for name in names):
        raise ValueError('会话工具子集含未知工具；请通过 /tools 修正')
    selected = set(names)
    # 始终遵循注册顺序，用户配置的顺序不打乱稳定前缀。
    return [row for row in available if row['function']['name'] in selected]


def selected_schemas(available: list[dict]) -> list[dict]:
    names = active_tool_names.get()
    return filter_schemas(available, config.MODEL_TOOL_NAMES if names == 'default' else names)


@contextmanager
def model_tools(names: list[str] | None):
    token = active_tool_names.set(names)
    try:
        yield
    finally:
        active_tool_names.reset(token)
