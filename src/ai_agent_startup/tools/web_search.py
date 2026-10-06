"""确认后联网搜索，限制等待时间和返回内容。"""
import json
import time
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from ai_agent_startup import config
from ai_agent_startup.tools.base import register_tool
from ai_agent_startup.tools.sandbox import ask_permission, audit, cancellation_requested, truncate


class WebSearchArgs(BaseModel):
    """联网检索标题、链接和摘要；调用前向用户展示完整查询并确认。"""
    query: str = Field(min_length=1, max_length=4000, description="将发送至 Tavily 的完整搜索关键词")
    max_results: int = Field(default=5, ge=1, le=10, description="结果数量，1 至 10")
    search_depth: Literal['basic', 'advanced'] = 'basic'


@register_tool(WebSearchArgs, name='web_search')
def web_search(query: str, max_results: int = 5, search_depth: str = 'basic') -> str:
    args = WebSearchArgs(query=query, max_results=max_results, search_depth=search_depth)
    if not config.WEB_SEARCH_API_KEY:
        return '[不可用] 请配置 WEB_SEARCH_API_KEY；不会使用无密钥模式。'
    from ai_agent_startup.tools.sandbox import current_policy
    if config.WEB_SEARCH_CONFIRM != 'off' or current_policy() in {'smart', 'full_access'}:
        if not ask_permission('web_search', query, '此查询将发送至 Tavily'):
            audit('web_search', query=query, status='denied')
            return '[已取消] 未发送网络查询。'
    if cancellation_requested():
        return '[已取消] 未发送网络查询。'
    try:
        deadline = time.monotonic() + config.WEB_SEARCH_TIMEOUT
        body = bytearray()
        with httpx.Client(timeout=config.WEB_SEARCH_TIMEOUT) as client:
            with client.stream('POST', 'https://api.tavily.com/search',
                               json={'api_key': config.WEB_SEARCH_API_KEY, **args.model_dump()}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    if cancellation_requested():
                        return '[已取消] 搜索已停止。'
                    if time.monotonic() > deadline:
                        raise httpx.ReadTimeout('搜索总时限')
                    if len(body) + len(chunk) > 2_000_000:
                        raise ValueError('搜索响应超过 2 MB 上限')
                    body.extend(chunk)
        data = json.loads(body)
        result = '\n'.join(f"- [{r.get('title', '')}]({r.get('url', '')}): {r.get('content', '')}"
                           for r in data.get('results', [])[:max_results])
        audit('web_search', query=query, status='completed', output_chars=len(result))
        return truncate(result)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        kind = '超时' if isinstance(exc, httpx.TimeoutException) else '失败'
        # 不向模型或日志暴露含密钥的请求体。
        audit('web_search', query=query, status='failed', error=type(exc).__name__)
        return f'[搜索{kind}] {type(exc).__name__}；请稍后重试或缩小查询。'
