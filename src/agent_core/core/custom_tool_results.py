"""解析动态工具的命令输出，区分进程退出与结构化业务结果。"""
import json
from typing import Literal


_STDOUT_PREFIX = '[退出码] 0\n[stdout]\n'


def custom_stdout_body(result: object) -> str | None:
    """仅返回零退出码回执的标准输出，标准错误不提供业务证据。"""
    if not isinstance(result, str) or not result.startswith(_STDOUT_PREFIX):
        return None
    return result[len(_STDOUT_PREFIX):].partition('\n[stderr]\n')[0]


def custom_result_is_business_error(value: object) -> bool:
    """根据已解析的结果识别明确的业务失败。"""
    if not isinstance(value, dict):
        return False
    if value.get('ok') is False or value.get('success') is False:
        return True
    status = value.get('status')
    if isinstance(status, str) and status.lower() in {'error', 'failed', 'failure', 'cancelled', 'denied'}:
        return True
    return bool(value.get('error')) and set(value) <= {'error', 'status', 'ok', 'success', 'code', 'message'}


def custom_result_outcome(result: object) -> Literal["completed", "business_error", "unknown"]:
    """区分完整业务回执、明确的业务失败与不满足该回执协议的输出。"""
    output = custom_stdout_body(result)
    if output is None:
        return 'unknown'

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('重复键')
            value[key] = item
        return value

    try:
        # 框架截断标记在 JSON 之外，整段解析会拒绝；字符串内的同样字面文本是业务数据。
        value = json.loads(output.strip(), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限数')))
    except (ValueError, TypeError, RecursionError):
        return 'unknown'
    return 'business_error' if custom_result_is_business_error(value) else 'completed'
