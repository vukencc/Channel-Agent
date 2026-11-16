"""当前会话摘要检索及原文小页读取；JSON 回执完整，归档原文不变。"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core import config
from agent_core.core.session_service import current_service
from agent_core.tools.base import register_tool
from agent_core.tools.sandbox import audit, cancellation_requested


HISTORY_TOOL_RESULT_CHARS = 1500
_SEARCH_SUMMARY_CHARS = 160
_READ_HINT = '仅摘要，不证明执行成功；按ID history_read，缩小limit分页。'


class HistorySearchArgs(BaseModel):
    """搜索当前会话归档摘要；摘要不证明工具成功，按命中 ID 用 history_read 核对原文。"""

    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20, strict=True)
    method: Literal['bm25', 'hybrid'] = 'bm25'


class HistoryReadArgs(BaseModel):
    """读取本会话归档原文的小页；按真实 next_offset 续读，最多附带两个邻块。"""

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
    if len(payload) > min(config.TOOL_MAX_OUTPUT, HISTORY_TOOL_RESULT_CHARS):
        raise ValueError('历史结果超过小页输出上限；请缩小 limit 或调整分页')
    return payload


def _search_json(rows: list[dict]) -> str:
    """先缩短真实候选摘要，再省略末位候选；不改变排名、元数据或输入。"""
    fields = {'ID', 'Time', 'Summary', 'score', 'previous_id', 'next_id', 'retrieval'}
    selected = [{key: value for key, value in row.items() if key in fields} for row in rows]
    maximum = min(config.TOOL_MAX_OUTPUT, HISTORY_TOOL_RESULT_CHARS)
    for row in selected:
        summary = row.get('Summary')
        if isinstance(summary, str) and len(summary) > _SEARCH_SUMMARY_CHARS:
            row['Summary'] = summary[:_SEARCH_SUMMARY_CHARS]
            row['summary_truncated'] = True

    def payload_size() -> int:
        return len(json.dumps(selected, ensure_ascii=False, separators=(',', ':')))

    def annotate() -> None:
        if selected:
            omitted = len(rows) - len(selected)
            if omitted:
                # 仅统计本次已排序候选中省略的条目，不能声称是全库命中数。
                selected[0]['omitted_count'] = omitted
            if omitted or any(row.get('summary_truncated') for row in selected):
                selected[0]['read_hint'] = _READ_HINT

    annotate()
    while len(selected) > 1 and payload_size() > maximum:
        selected.pop()
        annotate()
    if selected and payload_size() > maximum:
        row = selected[0]
        summary = row.get('Summary')
        if isinstance(summary, str):
            row['summary_truncated'] = True
            annotate()
            row['Summary'] = ''
            if payload_size() > maximum:
                raise ValueError('历史候选元数据超过小页输出上限，无法返回完整 JSON')
            low, high = 0, len(summary)
            while low < high:
                middle = (low + high + 1) // 2
                row['Summary'] = summary[:middle]
                if payload_size() <= maximum:
                    low = middle
                else:
                    high = middle - 1
            row['Summary'] = summary[:low]
    return _complete_json(selected)


@register_tool(HistorySearchArgs, name='history_search', concurrency='read')
def history_search(query: str, limit: int = 5, method: str = 'bm25') -> str:
    """返回真实排序的归档摘要小页与分数；需要证据时按 ID 读取原文及配对回执。"""
    if not query.strip():
        raise ValueError('query 不能为空')
    result = _run(lambda history, session: history.search(session, query.strip(), limit=limit, method=method))
    audit('read', action='history_search', count=len(result), method=method)
    return _search_json(result)


@register_tool(HistoryReadArgs, name='history_read', concurrency='read')
def history_read(chunk_id: str, before: int = 0, after: int = 0,
                 offset: int = 0, limit: int = 6000) -> str:
    """返回本会话归档原文的完整 JSON 小页；按 next_offset 继续，不截断已生成回执。"""
    if before + after > 2:
        raise ValueError('最多读取两个相邻历史片段')
    # 原文生产端按完整 JSON 上限选页；不在执行后裁剪字符串或伪报 next_offset。
    maximum = min(config.TOOL_MAX_OUTPUT, HISTORY_TOOL_RESULT_CHARS)
    limit = min(limit, max(1, maximum - 512))
    result = _run(lambda history, session: history.read(
        session, chunk_id, before=before, after=after, offset=offset, limit=limit,
        output_limit=maximum))
    audit('read', action='history_read', chunk_id=chunk_id,
          before=before, after=after, offset=offset)
    return _complete_json(result)
