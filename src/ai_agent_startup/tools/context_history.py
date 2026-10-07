"""Current-session archive search and bounded raw-history reads."""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_agent_startup import config
from ai_agent_startup.core.session_service import current_service
from ai_agent_startup.tools.base import register_tool
from ai_agent_startup.tools.sandbox import audit, cancellation_requested


class HistorySearchArgs(BaseModel):
    """Search this session's archived summaries, then read a selected ID for detail."""

    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20, strict=True)
    method: Literal['bm25', 'hybrid'] = 'bm25'


class HistoryReadArgs(BaseModel):
    """Read one owned archive chunk and at most two adjacent chunks."""

    model_config = ConfigDict(extra='forbid')
    chunk_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    before: int = Field(default=0, ge=0, le=2, strict=True)
    after: int = Field(default=0, ge=0, le=2, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=6000, ge=1, le=6000, strict=True)

    @model_validator(mode='after')
    def bounded_neighborhood(self):
        if self.before + self.after > 2:
            raise ValueError('最多读取两个相邻历史片段')
        return self


def _run(operation):
    context = current_service()
    if context.caller_id is None or context.loop is None:
        raise PermissionError('历史工具仅供运行中的会话使用')
    manager = context.manager
    history = getattr(manager, 'history_context', None)
    session = manager.sessions.get(context.caller_id)
    if history is None or session is None:
        raise PermissionError('当前会话没有可访问的历史归档')

    async def invoke():
        if cancellation_requested() or getattr(manager, 'closing', False):
            raise InterruptedError('历史读取已取消')
        result = await operation(history, session)
        if cancellation_requested() or getattr(manager, 'closing', False):
            raise InterruptedError('历史读取已取消')
        return result

    future = asyncio.run_coroutine_threadsafe(invoke(), context.loop)
    while True:
        try:
            return future.result(timeout=0.2)
        except FutureTimeoutError:
            if future.done():
                raise
            if cancellation_requested() or getattr(manager, 'closing', False):
                future.cancel()
                raise InterruptedError('历史读取已取消') from None


def _complete_json(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if len(payload) > config.TOOL_MAX_OUTPUT:
        raise ValueError('历史结果超过 TOOL_MAX_OUTPUT；请缩小 limit 或调整分页')
    return payload


def _search_json(rows: list[dict]) -> str:
    """Keep search output parseable when the configured tool limit is small."""
    fields = {'ID', 'Time', 'Summary', 'score', 'previous_id', 'next_id', 'retrieval'}
    selected = [{key: value for key, value in row.items() if key in fields} for row in rows]
    while len(selected) > 1 and len(json.dumps(selected, ensure_ascii=False, separators=(',', ':'))) > config.TOOL_MAX_OUTPUT:
        selected.pop()
    if selected and len(json.dumps(selected, ensure_ascii=False, separators=(',', ':'))) > config.TOOL_MAX_OUTPUT:
        row = selected[0]
        summary = row.get('Summary', '')
        if isinstance(summary, str):
            row['summary_truncated'] = True
            low, high = 0, len(summary)
            while low < high:
                middle = (low + high + 1) // 2
                row['Summary'] = summary[:middle]
                if len(json.dumps(selected, ensure_ascii=False, separators=(',', ':'))) <= config.TOOL_MAX_OUTPUT:
                    low = middle
                else:
                    high = middle - 1
            row['Summary'] = summary[:low]
    return _complete_json(selected)


@register_tool(HistorySearchArgs, name='history_search', concurrency='read')
def history_search(query: str, limit: int = 5, method: str = 'bm25') -> str:
    """Return current-session archive summaries and actual retrieval scores."""
    if not query.strip():
        raise ValueError('query 不能为空')
    result = _run(lambda history, session: history.search(session, query.strip(), limit=limit, method=method))
    audit('read', action='history_search', count=len(result), method=method)
    return _search_json(result)


@register_tool(HistoryReadArgs, name='history_read', concurrency='read')
def history_read(chunk_id: str, before: int = 0, after: int = 0,
                 offset: int = 0, limit: int = 6000) -> str:
    """Return a complete JSON page from an owned raw-history chunk."""
    if before + after > 2:
        raise ValueError('最多读取两个相邻历史片段')
    # Reserve space for JSON keys, metadata and quoting; the service applies
    # the final exact output budget while selecting the raw-history page.
    limit = min(limit, max(1, config.TOOL_MAX_OUTPUT - 512))
    result = _run(lambda history, session: history.read(
        session, chunk_id, before=before, after=after, offset=offset, limit=limit))
    audit('read', action='history_read', chunk_id=chunk_id,
          before=before, after=after, offset=offset)
    return _complete_json(result)
