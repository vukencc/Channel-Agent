"""会话工具子集：请求 schema 和执行侧使用同一允许列表。"""
import copy
from contextlib import contextmanager
from contextvars import ContextVar

from agent_core import config
from agent_core.core.session_limits import limit as session_limit


active_tool_names = ContextVar('active_tool_names', default='default')


def filter_schemas(available: list[dict], names: list[str] | None) -> list[dict]:
    if names is None:
        return available
    known = {row['function']['name'] for row in available}
    if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) or name not in known for name in names):
        raise ValueError('会话工具子集含未知工具；请修正内部允许列表配置')
    selected = set(names)
    # 始终遵循注册顺序，用户配置的顺序不打乱稳定前缀。
    return [row for row in available if row['function']['name'] in selected]


def model_catalog(available: list[dict], names: list[str] | None) -> list[dict]:
    """从执行注册表生成简洁模型目录；历史仅允许旧名的策略保留旧能力。"""
    selected = filter_schemas(available, names)
    allowed = {row['function']['name'] for row in selected}
    replacements = {'delegate': 'create_session', 'task_status': 'wait_agent_task',
                    'job_status': 'inspect_command_job', 'job_logs': 'inspect_command_job'}
    return [row for row in selected if replacements.get(row['function']['name']) not in allowed]


def _without_schema_titles(parameters):
    """只去掉 schema 注释；参数名及 default/enum 中的业务数据保持原样。"""
    result = copy.deepcopy(parameters)

    def strip(schema):
        if not isinstance(schema, dict):
            return
        schema.pop('title', None)
        for key in ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'):
            children = schema.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    strip(child)
        for key in ('items', 'additionalProperties', 'unevaluatedProperties', 'contains',
                    'not', 'if', 'then', 'else', 'propertyNames', 'additionalItems'):
            child = schema.get(key)
            if isinstance(child, list):
                for item in child:
                    strip(item)
            else:
                strip(child)
        for key in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
            children = schema.get(key)
            if isinstance(children, list):
                for child in children:
                    strip(child)

    strip(result)
    return result


def bounded_schemas(available: list[dict], names: list[str] | None) -> list[dict]:
    """请求与预算共用实际会话约束；只复制受影响项，不修改共享注册 schema。"""
    result = []
    parent_cap = session_limit('MAX_TOOL_ROUNDS')
    cap = min(256, parent_cap) if parent_cap > 0 else 0
    for row in model_catalog(available, names):
        if 'parameters' in row['function']:
            row = {**row, 'function': {**row['function'],
                   'parameters': _without_schema_titles(row['function']['parameters'])}}
        if row['function']['name'] in {'create_session', 'delegate'}:
            rounds = row['function']['parameters']['properties'].get('max_rounds')
            if rounds is not None:
                rounds['maximum'] = min(rounds.get('maximum', cap), cap)
                if type(rounds.get('default')) is int:
                    rounds['default'] = min(rounds['default'], rounds['maximum'])
                if parent_cap == 0:
                    rounds['enum'] = [0]
                    rounds['description'] = '父会话没有轮次上限；只能使用 0，继承无限轮次策略。'
        result.append(row)
    return result


def selected_schemas(available: list[dict]) -> list[dict]:
    names = active_tool_names.get()
    return bounded_schemas(available, config.MODEL_TOOL_NAMES if names == 'default' else names)


@contextmanager
def model_tools(names: list[str] | None):
    token = active_tool_names.set(names)
    try:
        yield
    finally:
        active_tool_names.reset(token)
