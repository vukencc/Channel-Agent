"""当前会话摘要检索及原文小页读取；JSON 回执完整，归档原文不变。"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import copy
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core import config
from agent_core.core.session_service import current_service
from agent_core.core.history_locations import TOOL_NAME_PATTERN
from agent_core.tools.base import register_tool
from agent_core.tools.sandbox import audit, cancellation_requested, _context as _tool_context


HISTORY_TOOL_RESULT_CHARS = 1500
_SEARCH_SUMMARY_CHARS = 160
_READ_HINT = '仅摘要，不证明成功；若history_read可用，按ID缩小limit分页。'
_LOCATION_HINT = ('仅定位配对回执，不证明成功；若history_read可用，read_args读取JSON编码原文片段；'
                  '按实际next_offset续读，limit不超过end_offset-next_offset，至end_offset停止。')
_NO_READER_HINT = '当前未启用history_read；定位不证明成功，也不承诺原文恢复。'


class HistorySearchArgs(BaseModel):
    """搜索归档摘要；可按真实配对回执工具名过滤。若 history_read 可用，按定位读取 JSON 编码原文。"""

    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20, strict=True)
    method: Literal['bm25', 'hybrid'] = 'bm25'
    tool_name: Annotated[str, Field(strict=True, pattern=TOOL_NAME_PATTERN)] | None = None


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
    session = manager.sessions.get(context.caller_id)
    selector = getattr(manager, 'context_for_session', None)
    history = (selector(session) if session is not None and callable(selector)
               else getattr(manager, 'history_context', None))
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


def _search_json(rows: list[dict], *, reader_available: bool | None = None) -> str:
    """先缩短真实候选摘要，再省略末位候选；不改变排名、元数据或输入。"""
    fields = {'ID', 'Time', 'Summary', 'score', 'previous_id', 'next_id', 'retrieval'}
    selected = [{key: value for key, value in row.items() if key in fields} for row in rows]
    maximum = min(config.TOOL_MAX_OUTPUT, HISTORY_TOOL_RESULT_CHARS)
    originals = {}
    for row, source in zip(selected, rows):
        if 'tool_results' in source:
            locators = source['tool_results']
            if not isinstance(locators, list) or any(not isinstance(value, dict) for value in locators):
                raise ValueError('历史回执定位格式无效')
            originals[id(row)] = locators
            row['tool_results'] = []
            if locators:
                row['tool_results'].append(copy.deepcopy(locators[0]))
        summary = row.get('Summary')
        if isinstance(summary, str) and len(summary) > _SEARCH_SUMMARY_CHARS:
            row['Summary'] = summary[:_SEARCH_SUMMARY_CHARS]
            row['summary_truncated'] = True

    def payload_size() -> int:
        return len(json.dumps(selected, ensure_ascii=False, separators=(',', ':')))

    def annotate() -> None:
        for row in selected:
            if id(row) in originals:
                omitted = len(originals[id(row)]) - len(row['tool_results'])
                if omitted:
                    row['tool_results_omitted_count'] = omitted
                else:
                    row.pop('tool_results_omitted_count', None)
                if reader_available is False:
                    for locator in row['tool_results']:
                        locator.pop('read_args', None)
        if selected:
            omitted = len(rows) - len(selected)
            if omitted:
                # 仅统计本次已排序候选中省略的条目，不能声称是全库命中数。
                selected[0]['omitted_count'] = omitted
            if any('tool_results' in row for row in selected):
                selected[0]['read_hint'] = _NO_READER_HINT if reader_available is False else _LOCATION_HINT
            elif omitted or any(row.get('summary_truncated') for row in selected):
                selected[0]['read_hint'] = _NO_READER_HINT if reader_available is False else _READ_HINT

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
    # 每个保留候选至少有一条完整定位；有空间才按原顺序补充更多真实定位。
    for row in selected:
        if id(row) not in originals:
            continue
        for locator in originals[id(row)][len(row['tool_results']):]:
            row['tool_results'].append(copy.deepcopy(locator))
            annotate()
            if payload_size() > maximum:
                row['tool_results'].pop()
                annotate()
                break
    return _complete_json(selected)


def _history_reader_available() -> bool | None:
    """按当前调用者的有效 schema 提供入口，不因定位元数据启用任何能力。"""
    context = current_service()
    manager = context.manager
    session = manager.sessions.get(context.caller_id) if manager is not None else None
    session_tools = getattr(manager, 'session_tools', None)
    if session is None:
        return False
    if not callable(session_tools):
        # 兼容没有 schema 查询接口的既有宿主；文案始终保留可用性条件。
        return None
    return any(item.get('name') == 'history_read' for item in session_tools(session))


@register_tool(HistorySearchArgs, name='history_search', concurrency='read')
def history_search(query: str, limit: int = 5, method: str = 'bm25',
                   tool_name: str | None = None) -> str:
    """返回摘要小页；可过滤真实工具回执，若 reader 可用则按定位及实际 next_offset 读取原文。"""
    if not query.strip():
        raise ValueError('query 不能为空')
    options = {'tool_name': tool_name} if tool_name is not None else {}
    result = _run(lambda history, session: history.search(session, query.strip(), limit=limit, method=method, **options))
    audit('read', action='history_search', count=len(result), method=method, **options)
    return _search_json(result, reader_available=_history_reader_available())


@register_tool(HistoryReadArgs, name='history_read', concurrency='read')
def history_read(chunk_id: str, before: int = 0, after: int = 0,
                 offset: int = 0, limit: int = 6000) -> str:
    """返回本会话归档原文的完整 JSON 小页；按 next_offset 继续，不截断已生成回执。"""
    if before + after > 2:
        raise ValueError('最多读取两个相邻历史片段')
    # 原文生产端按完整 JSON 上限选页；不在执行后裁剪字符串或伪报 next_offset。
    maximum = min(config.TOOL_MAX_OUTPUT, HISTORY_TOOL_RESULT_CHARS)
    args = {'chunk_id': chunk_id, 'before': before, 'after': after, 'offset': offset, 'limit': limit}
    effective_limit = min(limit, max(1, maximum - 512))
    tool_context = _tool_context.get()
    async def read_page(history, session):
        result = await history.read(session, chunk_id, before=before, after=after, offset=offset,
                                    limit=effective_limit, output_limit=maximum)
        payload = _complete_json(result)
        capture = getattr(history, 'capture_history_page', None)
        if tool_context is not None and capture is not None and tool_context.session_id == session.id:
            try:
                await capture(session, tool_context.tool_call_id, tool_context.turn_id, args, payload, result)
            except Exception:
                # 进度是可丢弃的提示，不能改变真实工具回执。
                pass
        return payload
    payload = _run(read_page)
    audit('read', action='history_read', chunk_id=chunk_id,
          before=before, after=after, offset=offset)
    return payload
