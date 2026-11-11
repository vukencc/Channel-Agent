"""节点结构化交接的受限定义与校验。"""
import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TYPES = {"string": str, "integer": int, "number": (int, float),
          "boolean": bool, "object": dict, "array": list, "null": type(None)}
MAX_HANDOFF_CHARS = 65536


def _matches(value, kind):
    expected = _TYPES[kind]
    return type(value) in (expected if isinstance(expected, tuple) else (expected,))


class HandoffField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["string", "integer", "number", "boolean", "object", "array", "null"]
    enum: list | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def check_enum(self):
        if self.enum is not None:
            if self.type in ("object", "array", "number"):
                raise ValueError("枚举仅支持标量")
            for item in self.enum:
                if not _matches(item, self.type) or (isinstance(item, float) and not math.isfinite(item)):
                    raise ValueError("枚举值类型无效")
                if isinstance(item, str) and len(item) > 256:
                    raise ValueError("枚举值过长")
                if isinstance(item, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                    raise ValueError("枚举字符串无效")
                if self.type == "integer" and abs(item) > 2**53 - 1:
                    raise ValueError("枚举整数超出安全范围")
        return self


class HandoffContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, HandoffField] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def check_fields(self):
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key) for key in self.fields):
            raise ValueError("交接字段名称无效")
        if len(json.dumps(self.model_dump(exclude_none=True), ensure_ascii=True)) > 8192:
            raise ValueError("交接契约过长")
        return self


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _depth(value, depth=0):
    if depth > 20:
        raise ValueError("depth")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("number")


def _exact_float(token: str) -> float:
    try:
        value = float(token)
        if not math.isfinite(value) or Decimal(token) != Decimal(str(value)):
            raise ValueError("number precision")
        return value
    except (InvalidOperation, OverflowError):
        raise ValueError("number precision") from None


def handoff_output_instruction(contract: dict) -> str:
    """将配置契约转成结果对象的约束，避免模型把 fields 配置层当作输出层。"""
    fields = {name: {key: value for key, value in spec.items() if value is not None}
              for name, spec in contract['fields'].items()}
    schema = {'type': 'object', 'properties': fields, 'required': list(fields),
              'additionalProperties': False}
    return ('\n最终交接只输出一个业务结果 JSON 对象，顶层字段必须且仅为：'
            + json.dumps(list(fields), ensure_ascii=True) + '。所有字段必填，不添加包装层。'
            + '\n下面是输出约束的 JSON Schema，请按它填写实际业务值：\n'
            + json.dumps(schema, ensure_ascii=True, separators=(',', ':')))


def validate_handoff(text: str, contract: dict) -> tuple[str, dict | None]:
    def failed(code, message):
        return "", {"status": "failed", "code": code, "message": message}

    if len(text) > MAX_HANDOFF_CHARS:
        return failed("output_too_large", "交接输出超过长度限制")
    source = text.strip()
    if source.startswith("```"):
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", source, re.DOTALL | re.IGNORECASE)
        if not match:
            return failed("invalid_json", "交接输出不是完整的 JSON")
        source = match.group(1)
    try:
        value = json.loads(source, object_pairs_hook=_unique,
                           parse_float=_exact_float,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
        _depth(value)
    except (ValueError, RecursionError):
        return failed("invalid_json", "交接输出不是有效的 JSON")
    fields = contract["fields"]
    if not isinstance(value, dict) or set(value) != set(fields):
        return failed("fields_mismatch", "交接字段与契约不符")
    for key, spec in fields.items():
        item = value[key]
        if not _matches(item, spec["type"]):
            return failed("type_mismatch", "交接字段类型不符")
        enum = spec.get("enum")
        if enum is not None and not any(item == choice for choice in enum):
            return failed("enum_mismatch", "交接字段不在允许的枚举中")
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False), None
