"""工具基础设施：Tool 抽象 + 注册表。这一层与任何具体工具无关。"""
from dataclasses import dataclass
from typing import Callable

from openai import pydantic_function_tool
from pydantic import BaseModel


@dataclass
class Tool:
    """一个工具 = 名字 + 参数模型 + 真正的执行函数。"""
    name: str
    args_model: type[BaseModel]
    fn: Callable[..., object]

    @property
    def schema(self) -> dict:
        # 参数模型 → 模型能看懂的工具说明书（pydantic 自动生成 JSON schema）
        return pydantic_function_tool(self.args_model, name=self.name) # type: ignore

    def run(self, arguments: str) -> str:
        # 用统一的参数模型校验模型传来的 JSON，再执行函数；类型错了会在这里暴露
        args = self.args_model.model_validate_json(arguments or "{}")
        return str(self.fn(**args.model_dump()))


# 全局注册表：名字 → Tool。主循环只认这张表，不认识任何具体工具
TOOL_REGISTRY: dict[str, Tool] = {}


def register_tool(args_model: type[BaseModel], name: str | None = None):
    """装饰器：把一个普通函数注册成工具，自动登记名字、schema 和执行函数。"""
    def decorator(fn: Callable[..., object]) -> Callable[..., object]:
        tool_name = name or fn.__name__
        TOOL_REGISTRY[tool_name] = Tool(name=tool_name, args_model=args_model, fn=fn)
        return fn
    return decorator
