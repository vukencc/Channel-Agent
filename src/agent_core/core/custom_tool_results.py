"""解析动态工具的命令输出，区分进程退出与结构化业务结果。"""
import json
from typing import Literal


def custom_result_outcome(result: object) -> Literal["completed", "business_error", "unknown"]:
    """区分完整业务回执、明确的业务失败与不满足该回执协议的输出。"""
    if not isinstance(result, str) or not result.startswith('[退出码] 0\n[stdout]\n'):
        return 'unknown'
    output = result[len('[退出码] 0\n[stdout]\n'):].split('\n[stderr]\n', 1)[0].strip()
    if '[输出已截断]' in output or '已截断）' in output:
        return 'unknown'
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('重复键')
            value[key] = item
        return value
    try:
        value = json.loads(output, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限数')))
    except (ValueError, TypeError, RecursionError):
        return 'unknown'
    if isinstance(value, dict):
        if value.get('ok') is False or value.get('success') is False:
            return 'business_error'
        if isinstance(value.get('status'), str) and value['status'].lower() in {'error', 'failed', 'failure', 'cancelled', 'denied'}:
            return 'business_error'
        if value.get('error') and set(value) <= {'error', 'status', 'ok', 'success', 'code', 'message'}:
            return 'business_error'
    return 'completed'
