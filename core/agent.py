"""通用 Agent 循环：不依赖任何具体工具，工具通过注册表动态查找。"""
import asyncio

import config
from core.llm import call_model, complete
from core.log import get_logger
from core.messages import AgentState, PackMessage
from rag.assess import assess_rag
from tools import TOOL_REGISTRY

logger = get_logger(__name__)

DEFAULT_PROMPT = (
    "You're a chatting robot to serve as a friend.\n"
    "使用 rag_search 检索本地知识库时：若返回的片段与问题无关、分数偏低或提示命中 0，"
    "先放宽 strictness（strict → normal → loose），仍不理想就改写 query 再试；"
    "同一问题最多重试 2 次。仍无结果就如实说明知识库中没有相关内容，不要凭猜测作答。"
)


async def assess_turn(query: str, answer: str, contexts: list[str]) -> None:
    """评估一轮：由 LLM 统一完成 RAG 评估并输出结果。评估失败不影响对话。"""
    try:
        result = await assess_rag(query, contexts, answer, complete)
        logger.info("[RAG评估]\n%s", result)
    except Exception:
        logger.exception("[RAG评估] 失败（不影响对话）")


async def session_loop(state: AgentState):
    history: list[dict] = [PackMessage("system", state.get("prompt"))]
    while True:
        user_input = input("You: ")
        history.append(PackMessage("user", user_input))

        contexts: list[str] = []  # 本轮 rag_search 返回的上下文，供评估使用

        # 模型可能先要求调用工具，执行完把结果喂回去，让它继续，
        # 直到它给出最终文字回复
        reply: dict | None = None
        while True:
            try:
                reply = await call_model(history)
            except Exception:
                # 重试后仍失败：记录日志并中止本轮，不影响后续对话
                logger.exception("模型调用失败，本轮中止")
                history.pop()  # 移除这条没被回答的 user 消息，保持历史干净
                reply = None
                break
            history.append(reply)

            tool_calls = reply.get("tool_calls")
            if not tool_calls:
                break
            for call in tool_calls:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                logger.debug("[tool] %s(%s)", name, arguments)
                # 主循环完全不认识具体工具：查注册表 → 统一校验执行 → 统一兜底
                tool = TOOL_REGISTRY.get(name)
                if tool is None:
                    logger.warning("模型请求了未知工具：%s", name)
                    result = f"未知工具: {name}"
                else:
                    try:
                        result = tool.run(arguments)
                    except Exception as e:
                        logger.warning("工具 %s 执行失败：%s", name, e)
                        result = f"工具执行失败: {e}"
                if name == "rag_search":
                    contexts.append(result)
                history.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })

        # 评估开关：True 时对本轮（问题, 检索上下文, 最终答案）自动打分
        if config.RAG_ASSESS and reply is not None and contexts:
            await assess_turn(user_input, reply.get("content", ""), contexts)


async def create_session(name: str, prompt: str = DEFAULT_PROMPT):
    session = asyncio.create_task(session_loop({"prompt": prompt}), name=name)
    await session
