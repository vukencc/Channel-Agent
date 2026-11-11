"""节点私有工具定义；代码仅由现有命令工具在所选执行环境中运行。"""
from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import shlex
from dataclasses import dataclass
from functools import lru_cache

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_CUSTOM_TOOLS = 16
MAX_SCHEMA_BYTES = 16384
MAX_ARGUMENT_BYTES = 65536
SCHEMA_KEYS = {
    "$schema", "type", "description", "title", "default", "properties", "required",
    "additionalProperties", "items", "enum", "const", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
    "minItems", "maxItems", "minProperties", "maxProperties",
}
SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


@lru_cache(maxsize=128)
def _check_schema(encoded: str) -> None:
    # 只缓存不可变内容的校验结论；改动 schema 后自动重验，不缓存节点能力。
    Draft202012Validator.check_schema(json.loads(encoded))


def _validate_parameters(value: dict) -> dict:
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("工具参数 schema 必须是有限 JSON") from None
    if len(encoded.encode()) > MAX_SCHEMA_BYTES:
        raise ValueError("工具参数 schema 超过 16 KiB")
    if value.get("type") != "object":
        raise ValueError("工具参数 schema 根类型必须为 object")
    pending = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if not isinstance(node, dict) or depth > 12 or count > 128:
            raise ValueError("工具参数 schema 层次或节点数超过限制")
        if set(node) - SCHEMA_KEYS:
            raise ValueError("工具参数 schema 含不支持的关键字，不支持引用、正则和组合 schema")
        if "type" in node and (not isinstance(node["type"], str) or node["type"] not in SCHEMA_TYPES):
            raise ValueError("工具参数 schema 只支持单一 JSON 类型")
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("工具参数 schema 仅支持 JSON Schema 2020-12")
        properties = node.get("properties", {})
        if (not isinstance(properties, dict) or len(properties) > 64
                or not all(isinstance(name, str) for name in properties)):
            raise ValueError("工具参数对象最多 64 个字段")
        pending.extend((child, depth + 1) for child in properties.values())
        if "items" in node:
            pending.append((node["items"], depth + 1))
        if "additionalProperties" in node and type(node["additionalProperties"]) is not bool:
            raise ValueError("additionalProperties 必须为布尔值")
        if "enum" in node and (not isinstance(node["enum"], list) or not 1 <= len(node["enum"]) <= 64):
            raise ValueError("工具参数 enum 必须含 1 到 64 个值")
    try:
        _check_schema(encoded)
    except (SchemaError, TypeError, ValueError):
        raise ValueError("工具参数 schema 无效") from None
    return value


class _ModuleMainBindings(ast.NodeVisitor):
    """查找模块作用域中可静态识别的 main 绑定。"""

    def __init__(self) -> None:
        self.bindings: list[ast.AST] = []

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "main" and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bindings.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name == "main":
            self.bindings.append(node)
        # 装饰器和默认值在外层求值，函数体属于局部作用域。
        for expression in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if expression is not None:
                self.visit(expression)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name == "main":
            self.bindings.append(node)
        for expression in [*node.decorator_list, *node.bases, *(kw.value for kw in node.keywords)]:
            self.visit(expression)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in [*node.args.defaults, *node.args.kw_defaults]:
            if expression is not None:
                self.visit(expression)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        # 推导式的迭代目标是局部变量，但表达式内的赋值表达式可绑定外层。
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)

    def visit_alias(self, node: ast.alias) -> None:
        if (node.asname or node.name.split(".")[0]) == "main":
            self.bindings.append(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name == "main":
            self.bindings.append(node)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name == "main":
            self.bindings.append(node)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name == "main":
            self.bindings.append(node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest == "main":
            self.bindings.append(node)
        self.generic_visit(node)


class CustomToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=2000)
    parameters: dict = Field(description="object JSON Schema，可含嵌套对象、数组、enum、required 和范围。不支持引用、正则或组合 schema。")
    code: str = Field(min_length=1, max_length=32000,
                      description="Python 源码，定义同步 main(arguments)，返回可 JSON 序列化的实际结果。通过项目命令执行环境运行。")
    network: bool = Field(default=False, strict=True)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict) -> dict:
        return _validate_parameters(value)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if len(value.encode('utf-8')) > 32000:
            raise ValueError("自定义工具 Python 代码超过 32000 UTF-8 字节")
        try:
            tree = ast.parse(value)
        except (SyntaxError, ValueError, RecursionError):
            raise ValueError("自定义工具的 Python 代码语法无效") from None
        entries = [item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "main"]
        bindings = _ModuleMainBindings()
        bindings.visit(tree)
        if len(entries) != 1 or bindings.bindings != entries:
            raise ValueError("工具代码必须定义一个同步 main(arguments) 函数")
        args = entries[0].args
        if (len(args.posonlyargs) + len(args.args) != 1 or args.vararg or args.kwarg
                or args.kwonlyargs or args.defaults):
            raise ValueError("main 必须只接收一个 arguments 参数")
        return value


def validate_custom_tools(values: list[dict] | None) -> list[dict]:
    from agent_core.tools import TOOL_REGISTRY

    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_CUSTOM_TOOLS:
        raise ValueError("每个节点最多定义 16 个自定义工具")
    result = [CustomToolSpec.model_validate(item).model_dump() for item in values]
    names = [item["name"] for item in result]
    if len(set(names)) != len(names):
        raise ValueError("节点的自定义工具名称重复")
    if set(names) & TOOL_REGISTRY.keys():
        raise ValueError("自定义工具不能覆盖基础工具或已注册工具")
    return result


@dataclass
class CustomTool:
    definition: dict
    concurrency: str = "serial"
    workspace_access: bool = True
    holds_read_slot: bool = True

    @property
    def name(self) -> str:
        return self.definition["name"]

    @property
    def timeout_s(self) -> float | None:
        from agent_core.tools import TOOL_REGISTRY

        return TOOL_REGISTRY["run_command"].timeout_s

    @property
    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.definition["description"],
            "parameters": copy.deepcopy(self.definition["parameters"]),
        }}

    def run(self, arguments: str) -> str:
        from agent_core.tools.command import run_command
        from agent_core.tools.sandbox import audit

        if len(arguments.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError("自定义工具参数超过 64 KiB")
        try:
            data = json.loads(arguments or "{}", parse_constant=_reject_constant)
            # 1e999 等标准 JSON 数值也可能在解析后溢出为 infinity，执行前拒绝。
            json.dumps(data, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("自定义工具参数不是有效 JSON") from None
        try:
            Draft202012Validator(self.definition["parameters"]).validate(data)
        except ValidationError as exc:
            location = "/".join(str(part) for part in exc.absolute_path)
            raise ValueError(f"自定义工具参数不符合 schema，字段 {location or '根对象'}") from None
        # Base64 避免 Unicode/引号膨胀。两份有界输入加固定包装小于 Linux 单 argv 上限。
        # exec 仅在现有命令隔离进程内执行，宿主只验证语法与参数。
        script = ("import base64,inspect,json,sys,types; "
                  "ns={'__name__':'__main__'}; "
                  "exec(compile(base64.b64decode(sys.argv[1]),'<node-tool>','exec'),ns); "
                  "entry=ns.get('main'); "
                  "params=list(inspect.signature(entry).parameters.values()) if type(entry) is types.FunctionType else []; "
                  "valid=type(entry) is types.FunctionType and not inspect.iscoroutinefunction(entry) and "
                  "len(params)==1 and params[0].kind in (inspect.Parameter.POSITIONAL_ONLY,inspect.Parameter.POSITIONAL_OR_KEYWORD) and "
                  "params[0].default is inspect.Parameter.empty\n"
                  "if not valid: raise TypeError('invalid main entry')\n"
                  "print(json.dumps(entry(json.loads(base64.b64decode(sys.argv[2]))),"
                  "ensure_ascii=False,allow_nan=False))")
        code = base64.b64encode(self.definition['code'].encode('utf-8')).decode('ascii')
        payload = base64.b64encode((arguments or '{}').encode('utf-8')).decode('ascii')
        command = "python3 -c " + shlex.quote(script) + " " + code + " " + payload
        audit("custom_tool_execution", tool=self.name,
              code_digest=hashlib.sha256(self.definition["code"].encode()).hexdigest(),
              network=self.definition["network"], strict=True)
        return run_command(command=command, reason=f"执行节点自定义工具 {self.name}",
                           network=self.definition["network"])


def _reject_constant(value: str):
    raise ValueError("参数包含非有限数字")


def custom_registry(values: list[dict] | None) -> dict[str, CustomTool]:
    return {item["name"]: CustomTool(item) for item in validate_custom_tools(values)}
