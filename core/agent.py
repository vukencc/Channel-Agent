"""通用 Agent 循环：不依赖任何具体工具，工具通过注册表动态查找。"""
import asyncio

from core.llm import call_model
from core.log import get_logger
from core.messages import AgentState, PackMessage
from tools import TOOL_REGISTRY

logger = get_logger(__name__)

DEFAULT_PROMPT = "You're a chatting robot to serve as a friend."


async def session_loop(state: AgentState):
    history: list[dict] = [PackMessage("system", state.get("prompt"))]
    while True:
        user_input = input("You: ")
        history.append(PackMessage("user", user_input))

        # 模型可能先要求调用工具，执行完把结果喂回去，让它继续，
        # 直到它给出最终文字回复
        while True:
            try:
                reply = await call_model(history)
            except Exception:
                # 重试后仍失败：记录日志并中止本轮，不影响后续对话
                logger.exception("模型调用失败，本轮中止")
                history.pop()  # 移除这条没被回答的 user 消息，保持历史干净
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
                history.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })


async def create_session(name: str, prompt: str = DEFAULT_PROMPT):
    session = asyncio.create_task(session_loop({"prompt": prompt}), name=name)
    await session
