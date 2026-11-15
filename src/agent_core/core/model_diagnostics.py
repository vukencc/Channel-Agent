"""沿用模型请求策略的无工具连接检查；仅返回安全状态。"""
import asyncio
import re
import time

import openai

from agent_core import config
from agent_core.core.budgets import BudgetExceeded, budget_scope
from agent_core.core.llm import ModelResponseError, RuntimeModelRequestError, selected_model
from agent_core.core.model_settings import model_fallbacks, model_profile as profile_scope
from agent_core.core.model_transport import ModelProxyError
from agent_core.core.session_limits import limits_scope
from agent_core.core.tool_settings import model_tools


def _failure(exc: Exception, phase: str | None) -> tuple[str, str]:
    if isinstance(exc, RuntimeModelRequestError) and exc.__context__ is not None:
        exc = exc.__context__
    if isinstance(exc, BudgetExceeded):
        return 'budget', '请求受费用预算或速率限制，请检查预算设置。'
    if isinstance(exc, (TimeoutError, openai.APITimeoutError)):
        return 'timeout', '模型连接或响应等待超时，请检查网络与模型服务。'
    if isinstance(exc, ModelProxyError):
        return 'proxy', '系统代理不可用或配置无效，请检查代理设置。'
    if isinstance(exc, (ConnectionError, openai.APIConnectionError)):
        if phase == 'stream':
            return 'stream', '模型响应过程中连接中断，请检查网络与代理。'
        return 'connect', '模型首段响应前连接失败，请检查网络与当前代理。'
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if code in (401, 403):
            return 'authentication', '模型服务拒绝授权，请检查密钥与访问权限。'
        if code in (400, 422):
            return 'unsupported_parameters', '模型服务拒绝请求参数，请检查模型及参数支持情况。'
        if code == 404:
            return 'not_found', '模型服务未找到地址或模型，请检查连接设置。'
        if code == 429:
            return 'rate_limit', '模型服务触发请求或额度限制，请稍后重试。'
        if 500 <= code < 600:
            return 'provider', '模型服务暂时不可用，请检查服务状态。'
    if isinstance(exc, ModelResponseError):
        return 'response', '模型未返回完整可用响应。'
    if isinstance(exc, (ValueError, TypeError)):
        return 'configuration', '模型配置无效，请检查模型及参数设置。'
    return 'unknown', '模型连接检查失败，请检查模型服务和连接设置。'


def _safe_model(value, key) -> str | None:
    # 模型标识可由用户配置，不能回显地址、查询字符串或凭据。
    if (isinstance(value, str) and not (key and key in value) and
            re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?', value)):
        return value[:200]
    return None


async def check_model_connection(manager, model_profile=None) -> dict:
    """返回 JSON 安全状态；取消继续传播，绝不返回模型内容或异常正文。"""
    started = time.monotonic()
    model = None
    proxy_mode = None
    phase = None

    def observe(kind, payload):
        nonlocal phase
        if kind == 'metrics' and isinstance(payload, dict) and payload.get('failure_phase') in ('connect', 'stream'):
            phase = payload['failure_phase']

    try:
        if model_profile and not manager.model_registry.contains(model_profile):
            raise ValueError('unknown profile')
        endpoint = manager.model_registry.resolve(model_profile)
        proxy_mode = endpoint.proxy_mode if endpoint else 'environment'
        with profile_scope(model_profile, endpoint), model_tools([]), model_fallbacks(False), budget_scope(manager.budget_ledger, 'model-connection-check'), limits_scope({'model_profile': model_profile}, endpoint=endpoint):
            model = _safe_model(selected_model(), endpoint.api_key if endpoint else config.API_KEY)
            async with asyncio.timeout(config.MODEL_CALL_TIMEOUT):
                async with manager.slots:
                    response = await manager.model([{'role': 'user', 'content': '请回复 OK。'}], session_id=None, emit=observe)
            if not isinstance(response, dict) or not isinstance(response.get('content'), str) or not response['content'] or response.get('tool_calls'):
                raise ModelResponseError('invalid response')
        category, message = None, '模型连接检查成功。'
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        category, message = _failure(exc, phase)
    return {'ok': category is None, 'status': 'ok' if category is None else 'error',
            'error_category': category, 'message': message, 'proxy_mode': proxy_mode,
            'model': model, 'elapsed_s': round(time.monotonic() - started, 3)}
