"""LLM 客户端与单次流式调用。

只负责「和模型说一次话」：发消息、收流、把 content 和 tool_calls 拼好返回。
不认识任何具体工具，也不管工具循环。
"""
import asyncio
import uuid

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


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.API_KEY,
            base_url=config.BASE_URL,
            default_headers={"x-opencode-session": str(uuid.uuid4())},
            max_retries=0,
            timeout=config.TIMEOUT,
        )
    return _client

# 从工具注册表派生，新增工具后这里不用改
TOOL_SCHEMAS = all_schemas()

# 评估调用的超时：评估要模型输出结构化评分，实测比普通对话慢得多
_ASSESS_TIMEOUT = 120.0

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
    async def operation():
        return await get_client().with_options(
            timeout=config.SESSION_TIMEOUT
        ).chat.completions.create(
            model=config.MODEL,
            messages=history,
            stream=True,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            extra_headers={"x-opencode-session": session_id} if session_id else None,
        )

    return await _retry(operation, "模型调用")


async def call_model(history: list[dict], *, session_id: str | None = None, emit=None) -> dict:
    stream = await _open_stream(history, session_id)

    ai_message = ""
    tool_calls: dict[int, dict] = {}

    # 这是给用户看的对话内容，不是日志，所以走 stdout
    if emit is None:
        print("AI: ", end="")
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                if emit is None:
                    print(delta.content, end="", flush=True)
                else:
                    emit("content", delta.content)
                ai_message += delta.content
            if emit and getattr(delta, "reasoning_content", None):
                emit("status", "模型正在思考")
            # 模型决定调用工具时，内容在 delta.tool_calls 里，而且是分片传的
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    if tc.index not in tool_calls:
                        tool_calls[tc.index] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls[tc.index]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_calls[tc.index]["name"] = tc.function.name
                        if emit:
                            emit("status", "准备调用 " + tc.function.name)
                    if tc.function and tc.function.arguments:
                        tool_calls[tc.index]["arguments"] += tc.function.arguments
    finally:
        await stream.close()
    if emit is None:
        print()

    if tool_calls:
        logger.debug(
            "收到 %d 个工具调用：%s",
            len(tool_calls),
            [c["name"] for c in tool_calls.values()],
        )

    message: dict = PackMessage("assistant", ai_message)
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"]},
            }
            for c in tool_calls.values()
        ]
    return message


async def complete(prompt: str, *, session_id: str | None = None) -> str:
    """
    非流式调用一次，返回完整文本。

    供评估（LLM 当裁判）等内部用途使用：不打印、不参与会话历史。
    评估要模型输出结构化评分，耗时明显长于对话（实测 10~30s），
    所以用更宽的超时、并且只重试一次，避免慢调用反复重试。
    """
    async def operation():
        return await get_client().with_options(
            timeout=_ASSESS_TIMEOUT
        ).chat.completions.create(
            model=config.MODEL,
            messages=[PackMessage("user", prompt)],
            stream=False,
            extra_headers={"x-opencode-session": session_id} if session_id else None,
        )

    response = await _retry(operation, "评估调用", attempts=2)
    return response.choices[0].message.content or ""
