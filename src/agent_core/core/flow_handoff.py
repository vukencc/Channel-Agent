"""节点结构化交接的受限定义与校验。"""
import json
import hashlib
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


def _failed(code: str, message: str) -> dict:
    return {"status": "failed", "code": code, "message": message}


def _decode_handoff(text: str) -> tuple[object, dict | None]:
    def failed(code, message):
        return None, _failed(code, message)

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
    return value, None


def _field_error(value: dict, fields: dict) -> dict | None:
    for key, spec in fields.items():
        if key not in value:
            continue
        item = value[key]
        if not _matches(item, spec["type"]):
            return _failed("type_mismatch", "交接字段类型不符")
        enum = spec.get("enum")
        if enum is not None and not any(item == choice for choice in enum):
            return _failed("enum_mismatch", "交接字段不在允许的枚举中")
    return None


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False)


def validate_handoff(text: str, contract: dict) -> tuple[str, dict | None]:
    value, failure = _decode_handoff(text)
    if failure:
        return "", failure
    fields = contract["fields"]
    if not isinstance(value, dict) or set(value) != set(fields):
        return "", _failed("fields_mismatch", "交接字段与契约不符")
    failure = _field_error(value, fields)
    return ("", failure) if failure else (_canonical(value), None)


def handoff_for_model(output: str, contract: dict) -> str:
    """只将已验收的规范 JSON 转为可读文本，输入预算按实际字符计。"""
    # 一个 Unicode 字符序列化成两个 JSON 代理转义时最多占 12 个 ASCII 字符。
    if len(output) > 12 * MAX_HANDOFF_CHARS:
        return output
    try:
        candidate = json.dumps(json.loads(output), ensure_ascii=False, separators=(",", ":"),
                               sort_keys=True, allow_nan=False)
        candidate.encode("utf-8")
    except (ValueError, RecursionError):
        return output
    canonical, failure = validate_handoff(candidate, contract)
    return candidate if failure is None and canonical == output else output


def receipt_field_correction(text: str, contract: dict, expected: dict) -> tuple[dict, set[str]] | None:
    """只允许原模型补正有真实回执支持的签发编号字段，其他结构错误沿用契约拒绝。"""
    if not expected:
        return None
    value, failure = _decode_handoff(text)
    fields = contract["fields"]
    if failure or not isinstance(value, dict) or not value or value.keys() - fields.keys():
        return None
    missing = fields.keys() - value.keys()
    if missing - expected.keys() or _field_error(value, fields):
        return None
    corrections = set(missing)
    for field, issued in expected.items():
        if field not in value:
            continue
        claimed = value[field]
        if (not isinstance(claimed, list) or len(claimed) != len(issued)
                or any(not isinstance(item, str) for item in claimed) or set(claimed) != set(issued)):
            corrections.add(field)
    return (value, corrections) if corrections else None


def preserves_handoff_fields(original: dict, corrected: dict, editable: set[str]) -> bool:
    """编号修正可在 notes 追加回执说明，其他已有业务字段逐项保持（区分 false 与 0）。"""
    for key, previous in original.items():
        if key in editable:
            continue
        current = corrected.get(key)
        if key == "notes" and isinstance(previous, str) and isinstance(current, str) and current.startswith(previous):
            continue
        if key not in corrected or _canonical(previous) != _canonical(current):
            return False
    return True


def receipt_patch_instruction(editable: set[str], *, can_append_notes: bool) -> str:
    """向当前内部编辑请求说明局部格式，最终交接仍按节点原契约验收。"""
    names = sorted(editable)
    properties = {"set": {"type": "object", "required": names, "additionalProperties": False,
                          "properties": {name: {"type": "array", "items": {"type": "string"}} for name in names}}}
    if can_append_notes:
        properties["append_notes"] = {"type": "string"}
    schema = {"type": "object", "properties": properties, "required": ["set"], "additionalProperties": False}
    return ("本次调用处于回执字段修正阶段，只输出一份符合以下 JSON Schema 的编辑对象。"
            "你提交实际签发的编号值，需要时提供追加说明。框架沿用原稿的其他字段，"
            "组装后的完整交接仍须通过原节点的交接契约和来源核对。\n"
            + json.dumps(schema, ensure_ascii=True, separators=(",", ":")))


def apply_receipt_patch(original: dict, text: str, editable: set[str], contract: dict) -> tuple[str, dict | None]:
    """组装模型明确提交的局部更改，并校验完整交接。"""
    if text.strip().startswith("```"):
        return "", _failed("invalid_json", "交接局部修正必须是严格 JSON")
    patch, failure = _decode_handoff(text)
    if failure:
        return "", failure
    if (not isinstance(patch, dict) or set(patch) - {"set", "append_notes"}
            or not isinstance(patch.get("set"), dict) or set(patch["set"]) != editable
            or any(not isinstance(v, list) or any(not isinstance(x, str) for x in v) for v in patch["set"].values())
            or ("append_notes" in patch and (not isinstance(original.get("notes"), str)
                                             or not isinstance(patch["append_notes"], str)))):
        return "", _failed("fields_mismatch", "交接局部修正字段无效")
    result = dict(original)
    result.update(patch["set"])
    if "append_notes" in patch:
        result["notes"] += patch["append_notes"]
    return validate_handoff(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False), contract)


def handoff_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def verified_derived_handoff(messages: list, last_run: dict, previous_count: int, contract: dict) -> tuple[str, dict | None]:
    """重建并绑定已审核的原稿、局部回复和完整交接。"""
    derived = last_run.get("handoff_derived")
    invalid = _failed("invalid_json", "已审核交接与原始消息不一致")
    if (not isinstance(derived, dict) or derived.get("turn_id") != last_run.get("turn_id")
            or last_run.get("handoff_finalization", {}).get("status") != "passed"
            or last_run.get("handoff_validation_failed")
            or type(derived.get("reply_index")) is not int
            or derived["reply_index"] != len(messages) - 1
            or type(derived.get("original_index")) is not int
            or not previous_count < derived["original_index"] < derived["reply_index"]
            or not isinstance(derived.get("editable"), list)
            or not derived["editable"]
            or any(not isinstance(key, str) for key in derived["editable"])
            or len(set(derived["editable"])) != len(derived["editable"])):
        return "", invalid
    original_message = messages[derived["original_index"]]
    patch_message = messages[derived["reply_index"]]
    if any(not isinstance(m, dict) or m.get("role") != "assistant" or m.get("tool_calls")
           or not isinstance(m.get("content"), str) for m in (original_message, patch_message)):
        return "", invalid
    original_text, patch_text = original_message["content"], patch_message["content"]
    if (handoff_digest(original_text) != derived.get("original_digest")
            or handoff_digest(patch_text) != derived.get("patch_digest")):
        return "", invalid
    original, failure = _decode_handoff(original_text)
    if failure or not isinstance(original, dict):
        return "", invalid
    output, failure = apply_receipt_patch(original, patch_text, set(derived["editable"]), contract)
    if failure:
        return "", invalid
    readable = json.dumps(json.loads(output), ensure_ascii=False, separators=(",", ":"))
    if handoff_digest(readable) != derived.get("result_digest"):
        return "", invalid
    return output, None
