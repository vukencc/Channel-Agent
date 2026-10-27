"""只读且有界的上下文状态；不加载历史，不暴露摘要、原文或服务异常。"""
import math
import re


def public_run_error(error) -> str:
    """保留安全配置项名称，不把供应商正文或配置值发送到页面。"""
    if isinstance(error, str) and error.startswith(('APIConnectionError:', 'ModelConnectionError:')):
        return '模型服务连接失败，请检查服务、网络和代理；任务记录已保留，可稍后发送“继续”。'
    if isinstance(error, str) and error.startswith('ValueError:'):
        error = error[:4096]
        names = re.findall(r'\b(?:BASE_URL|NO_PROXY|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|MODEL_FALLBACKS)\b', error)
        if names:
            codes = re.findall(r'\bU\+[0-9A-F]{4,6}\b', error)[:8]
            characters = ('；非法字符 ' + '、'.join(dict.fromkeys(codes))) if codes else ''
            return '配置错误：请检查 ' + '、'.join(dict.fromkeys(names)) + characters + '；配置值不在页面显示。'
    return '任务运行失败，请检查本地日志后重试。'


def context_status(session, history_context) -> dict:
    run = session.record.get('last_run', {})
    run = run if isinstance(run, dict) else {}
    metrics = run.get('recovery_context') or run.get('context') or {}
    metrics = metrics if isinstance(metrics, dict) else {}
    result = {'available': False, 'structured': metrics.get('structured') is True,
              'supervisor_enabled': False}

    def number(key, value):
        if type(value) in (int, float) and 0 <= value <= 10 ** 12 and math.isfinite(value):
            result[key] = value

    for key in ('original_chars', 'original_tokens', 'sent_chars', 'sent_tokens',
                'total_chars', 'reserved_chars', 'reserved_tokens', 'utilization',
                'archived_chunks', 'context_revision', 'session_summary_chars',
                'omitted_turns', 'compacted_file_parts', 'value_variance',
                'before_utilization', 'target_utilization', 'new_history_groups'):
        number(key, metrics.get(key))
    for key in ('input_limit_tokens', 'input_limit_chars', 'context_capacity_tokens',
                'response_reserve_tokens', 'schema_tokens', 'estimated_input_utilization', 'capacity_utilization'):
        number(key, metrics.get(key))
    # 供应商 usage 代表最近完成请求，与当前准备中的输入估算分开展示。
    calls = run.get('model_calls', [])
    if isinstance(calls, list):
        for call in reversed(calls):
            if isinstance(call, dict) and call.get('tokens_source') == 'provider':
                number('provider_input_tokens', call.get('input_tokens'))
                break
    if type(metrics.get('compacted')) is bool:
        result['compacted'] = metrics['compacted']
    supervisor = metrics.get('supervisor')
    if isinstance(supervisor, str):
        result['supervisor'] = ('unavailable' if supervisor.startswith('unavailable:')
                                else supervisor if supervisor in {
                                    'evaluated', 'already_compacted', 'cooldown', 'hard_budget', 'not_due', 'disabled', 'insufficient_history', 'unavailable', 'below_threshold'}
                                else 'unknown')
    for key, allowed in {
        'summary_kind': {'none', 'model', 'extractive_fallback'},
        'compact_reason': {'', 'budget_risk', 'value_variance', 'context_insufficient', 'high_utilization_and_variance'},
        'summary': {'pending', 'applied', 'failed_or_over_budget'},
    }.items():
        value = metrics.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    if history_context is not None:
        result['structured'] = True
        settings = getattr(getattr(history_context, 'supervisor', None), 'settings', None)
        result['supervisor_enabled'] = getattr(settings, 'enabled', False) is True
        cache = getattr(history_context, 'cache', {})
        manifest = cache.get(session.id, {}) if isinstance(cache, dict) else {}
        if isinstance(manifest, dict):
            number('context_revision', manifest.get('revision'))
            chunks = manifest.get('chunks')
            if isinstance(chunks, list):
                number('archived_chunks', len(chunks))
            kind = manifest.get('summary_kind')
            if isinstance(kind, str) and kind in {'none', 'model', 'extractive_fallback'}:
                result['summary_kind'] = kind
        checks = getattr(history_context, 'checks', {})
        if isinstance(checks, dict):
            number('check_count', checks.get(session.id))
    result['available'] = any(key in result for key in ('sent_tokens', 'sent_chars', 'archived_chunks'))
    return result
