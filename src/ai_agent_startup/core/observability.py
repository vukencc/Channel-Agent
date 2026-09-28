"""保留窗口内的只读指标；不载入消息或审计正文，不把未知费用记成零。"""
import math

from ai_agent_startup import config

_MODEL_FIELDS = {'model', 'total_s', 'input_chars', 'input_tokens', 'output_tokens', 'cached_input_tokens',
                 'tokens_source', 'estimated_cost_usd', 'outcome', 'error_type', 'finish_reason', 'max_chunk_gap_s'}
_TOOL_FIELDS = {'name', 'tool_call_id', 'wall_s_including_confirmation', 'output_chars'}
_CONTEXT_FIELDS = {'original_chars', 'sent_chars', 'original_tokens', 'sent_tokens', 'schema', 'memory', 'extra',
                   'total_chars', 'compacted_file_parts', 'omitted_turns', 'summary'}


def retained_runs(record):
    if not config.ENABLE_OBSERVABILITY:
        raise ValueError('请显式开启 ENABLE_OBSERVABILITY')
    runs = {}
    for index, row in enumerate(record.get('run_history', [])[-config.TRACE_MAX_ROWS:]):
        runs[row.get('turn_id') or f'legacy-{index}'] = row
    latest = record.get('last_run')
    if latest:
        runs[latest.get('turn_id') or 'latest'] = latest
    return list(runs.values())[-config.TRACE_MAX_ROWS:]


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def usage_report(record):
    runs = retained_runs(record)
    calls = [call for row in runs for call in row.get('model_calls', [])]
    known = [call['estimated_cost_usd'] for call in calls if _number(call.get('estimated_cost_usd'))]
    return {'scope': '当前会话保留轮次窗口，非账单或终生总额；tokens 可能含本地估计',
            'retained_turns': len(runs), 'model_calls': len(calls),
            'provider_usage_calls': sum(call.get('tokens_source') == 'provider' for call in calls),
            'estimated_usage_calls': sum(call.get('tokens_source') != 'provider' for call in calls),
            'input_tokens': sum(call.get('input_tokens', 0) for call in calls if _number(call.get('input_tokens'))),
            'output_tokens': sum(call.get('output_tokens', 0) for call in calls if _number(call.get('output_tokens'))),
            'known_cost_usd': sum(known), 'unknown_cost_calls': len(calls) - len(known),
            'total_cost_usd': sum(known) if len(known) == len(calls) else None,
            'tool_calls': sum(len(row.get('tools', [])) for row in runs)}


def _fields(row, allowed):
    from ai_agent_startup.core.headless import redact_result
    result = {}
    for key in allowed:
        value = row.get(key)
        if isinstance(value, str):
            result[key] = redact_result(value)[:config.TRACE_MAX_FIELD_CHARS]
        elif value is None or type(value) is bool or _number(value):
            if key in row:
                result[key] = value
    return result


def trace_report(record, prefix=''):
    rows = retained_runs(record)
    if prefix:
        rows = [row for row in rows if str(row.get('turn_id', '')).startswith(prefix)]
        if len(rows) != 1:
            raise ValueError('请指定保留窗口内唯一的轮次 ID 前缀')
    return [{**_fields(row, {'turn_id', 'status', 'total_s', 'stop_reason', 'background_summary'}),
             'context': _fields(row.get('context', {}), _CONTEXT_FIELDS),
             'model_calls': [_fields(call, _MODEL_FIELDS) for call in row.get('model_calls', [])[-config.TRACE_MAX_ROWS:]],
             'tools': [_fields(call, _TOOL_FIELDS) for call in row.get('tools', [])[-config.TRACE_MAX_ROWS:]]}
            for row in rows]
