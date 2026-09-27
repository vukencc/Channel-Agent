"""LLM 客户端与单次流式调用。

只负责「和模型说一次话」：发消息、收流、把 content 和 tool_calls 拼好返回。
不认识任何具体工具，也不管工具循环。
"""
import asyncio
import uuid
import json
import time
import threading

import httpx

import openai
from openai import AsyncOpenAI

import config
from core.log import get_logger
from core.messages import PackMessage
from tools import all_schemas

logger = get_logger(__name__)

# 重试策略由下面的 _open_stream 统一负责，这里关掉 SDK 自带的，避免双重重试。
# 客户端延迟创建：导入本模块不应要求 API key，也不产生副作用。
_client: AsyncOpenAI | None = None
_client_lock = threading.Lock()


def get_client() -> AsyncOpenAI:
    global _client
    with _client_lock:
        if _client is None:
            _client = AsyncOpenAI(
                api_key=config.API_KEY,
                base_url=config.BASE_URL,
                default_headers={"x-opencode-session": str(uuid.uuid4())},
                max_retries=0,
                timeout=config.TIMEOUT,
            )
        # SDK resources are lazy imports too; resolve them in this worker.
        _client.chat.completions
    return _client

# 从工具注册表派生，新增工具后这里不用改
TOOL_SCHEMAS = all_schemas()

class ModelResponseError(RuntimeError):
    """A response was not executable; a bounded smaller-step recovery is safe."""

def model_options() -> dict:
    """Explicit provider options; do not silently retry with different semantics."""
    options = {}
    if config.REASONING_EFFORT and config.THINKING_MODE != "disabled":
        options['reasoning_effort'] = config.REASONING_EFFORT
    if config.THINKING_MODE != "auto":
        options['extra_body'] = {'thinking': {'type': config.THINKING_MODE}}
    return options


# 连接类/限流/服务端错误，属于瞬时故障，可以重试
_RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _is_transient(exc: Exception) -> bool:
    """判断异常是否值得重试。

    这个网关偶发返回「空 body 的 422」，属于瞬时故障，值得重试；
    但带了 body 的 422 是真正的请求参数错误，重试没用，直接抛出。
    """
    if isinstance(exc, _RETRYABLE):
        return True
    if isinstance(exc, openai.APIStatusError):
        body = exc.response.text.strip() if exc.response is not None else ""
        return exc.status_code >= 500 or (exc.status_code == 422 and not body)
    return False


async def _retry(operation, what: str, attempts: int | None = None):
    """执行一次「瞬时错误可重试」的调用，按指数退避重试。"""
    attempts = max(1, attempts if attempts is not None else config.MAX_RETRIES)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if not _is_transient(exc):
                raise
            last_exc = exc
            if attempt < attempts:
                delay = min(2 ** (attempt - 1), 8)
                logger.warning(
                    "%s失败（第 %d/%d 次），%.0fs 后重试：%s",
                    what, attempt, attempts, delay, exc,
                )
                await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


async def _open_stream(history: list[dict], session_id: str | None = None):
    """发起一次流式请求；遇到瞬时错误按指数退避重试。"""
    # TLS/HTTP client initialization performs synchronous filesystem work.
    client = await asyncio.to_thread(get_client)
    async def operation():
        return await client.with_options(
            timeout=config.SESSION_TIMEOUT
        ).chat.completions.create(
            model=config.MODEL,
            **model_options(),
            messages=history,
            stream=True,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            extra_headers={"x-opencode-session": session_id} if session_id else None,
        )

    return await _retry(operation, "模型调用")


async def call_model(history: list[dict], *, session_id: str | None = None, emit=None) -> dict:
    """Bound retries + stream by a wall-clock deadline; never execute partial calls."""
    started = time.monotonic()
    metrics = {'input_chars': len(json.dumps(history, ensure_ascii=False)),
               'reasoning_chars': 0, 'content_chars': 0, 'argument_chars': 0}
    stream = None
    content, reasoning = [], []
    tool_calls: dict[int, dict] = {}
    finish = None
    progress_at = 0.0
    previous_chunk_at = started
    try:
        async with asyncio.timeout(config.MODEL_CALL_TIMEOUT):
            stream = await _open_stream(history, session_id)
            if emit is None:
                print("AI: ", end="")
            async for chunk in stream:
                if not chunk.choices:
                    continue
                elapsed = time.monotonic() - started
                metrics['max_chunk_gap_s'] = round(max(metrics.get('max_chunk_gap_s', 0),
                    time.monotonic() - previous_chunk_at), 3)
                previous_chunk_at = time.monotonic()
                metrics.setdefault('first_chunk_s', round(elapsed, 3))
                choice = chunk.choices[0]
                finish = getattr(choice, 'finish_reason', None) or finish
                delta = choice.delta
                thought = getattr(delta, 'reasoning_content', None)
                if thought:
                    reasoning.append(thought)
                    metrics['reasoning_chars'] += len(thought)
                if delta.content:
                    content.append(delta.content)
                    metrics['content_chars'] += len(delta.content)
                    if emit:
                        emit('content', delta.content)
                    else:
                        print(delta.content, end='', flush=True)
                if delta.content or delta.tool_calls:
                    metrics.setdefault('first_visible_s', round(elapsed, 3))
                for tc in delta.tool_calls or []:
                    call = tool_calls.setdefault(tc.index, {'id': '', 'name': '', 'arguments': []})
                    if tc.id:
                        call['id'] = tc.id
                    if tc.function and tc.function.name:
                        call['name'] = tc.function.name
                    if tc.function and tc.function.arguments:
                        call['arguments'].append(tc.function.arguments)
                        metrics['argument_chars'] += len(tc.function.arguments)
                if sum(metrics[key] for key in ('content_chars', 'reasoning_chars', 'argument_chars')) > config.MODEL_OUTPUT_CHARS:
                    raise ModelResponseError('模型单次输出超过字符上限，未执行工具；请拆分为小步骤。')
                if emit and elapsed - progress_at >= .25:
                    if delta.tool_calls:
                        names = ', '.join(c['name'] for c in tool_calls.values())
                        emit('status', f"生成工具参数：{names} · {metrics['argument_chars']} 字符")
                    elif thought:
                        emit('status', f"模型思考中 · 已接收 {metrics['reasoning_chars']} 字符")
                    elif delta.content:
                        emit('status', '模型正在回答')
                    progress_at = elapsed
            if finish not in {'stop', 'tool_calls'}:
                raise ModelResponseError(f'模型输出未完整结束（finish_reason={finish}），未执行工具；请重试或缩小任务。')
            message: dict = PackMessage('assistant', ''.join(content))
            # Required by thinking-mode providers on subsequent tool turns.
            if reasoning:
                message['reasoning_content'] = ''.join(reasoning)
            if tool_calls:
                calls, ids = [], set()
                for _, call in sorted(tool_calls.items()):
                    arguments = ''.join(call['arguments'])
                    if not call['id'] or not call['name'] or call['id'] in ids:
                        raise ModelResponseError('模型返回不完整或重复的工具标识，未执行工具。')
                    ids.add(call['id'])
                    try:
                        if not isinstance(json.loads(arguments), dict):
                            raise ValueError('工具参数必须是 JSON 对象')
                    except ValueError as exc:
                        raise ModelResponseError('模型返回无效工具 JSON，未执行工具。') from exc
                    calls.append({'id': call['id'], 'type': 'function',
                                  'function': {'name': call['name'], 'arguments': arguments}})
                message['tool_calls'] = calls
            elif not message['content']:
                raise ModelResponseError('模型没有返回回答或工具调用，请重试。')
            metrics['outcome'] = 'ok'
            return message
    except (TimeoutError, httpx.TimeoutException, openai.APITimeoutError) as exc:
        metrics['outcome'] = 'timeout'
        metrics['error_type'] = type(exc).__name__
        raise TimeoutError(
            f'模型请求在 {time.monotonic() - started:.1f}s 后超时（总时限 {config.MODEL_CALL_TIMEOUT:g}s，网络空闲时限 {config.SESSION_TIMEOUT:g}s）。'
            '已停止本次请求，未执行未完成的工具调用；可重试或缩小任务。'
        ) from exc
    finally:
        if stream is not None:
            await stream.close()
        metrics.update(total_s=round(time.monotonic() - started, 3), finish_reason=finish)
        metrics.setdefault('outcome', 'interrupted')
        logger.info('model_metrics session=%s %s', session_id, json.dumps(metrics))
        if emit:
            if reasoning:
                emit('reasoning', ''.join(reasoning))
            emit('metrics', metrics)
        else:
            print()


async def complete(prompt: str, *, session_id: str | None = None) -> str:
    """
    非流式调用一次，返回完整文本。

    供评估（LLM 当裁判）等内部用途使用：不打印、不参与会话历史。
    评估是可选后台工作，受独立总时限约束，不自动重试。
    """
    async def operation():
        client = await asyncio.to_thread(get_client)
        return await client.with_options(
            timeout=config.ASSESS_TIMEOUT
        ).chat.completions.create(
            model=config.MODEL,
            **model_options(),
            messages=[PackMessage("user", prompt)],
            stream=False,
            extra_headers={"x-opencode-session": session_id} if session_id else None,
        )

    async with asyncio.timeout(config.ASSESS_TIMEOUT):
        response = await _retry(operation, "评估调用", attempts=1)
    return response.choices[0].message.content or ""
