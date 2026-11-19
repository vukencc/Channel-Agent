"""Flow 本轮动作草稿；不执行工具，也不保存参数原文。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from collections import deque

from jsonschema import Draft202012Validator

from agent_core.core.custom_tools import MAX_ARGUMENT_BYTES


class _Integer(int):
    def __new__(cls, token: str):
        value = super().__new__(cls, token)
        value.token = token
        return value


class _Float(float):
    def __new__(cls, token: str):
        value = super().__new__(cls, token)
        value.token = token
        return value


def _argument_digest(value: object) -> str:
    # 数字的原始拼写也参与匹配；仅对象键顺序和空白可变化。
    def tagged(item: object) -> object:
        if isinstance(item, (_Integer, _Float)):
            return ["number", item.token]
        if isinstance(item, dict):
            return ["object", {key: tagged(child) for key, child in item.items()}]
        if isinstance(item, list):
            return ["array", [tagged(child) for child in item]]
        return [type(item).__name__, item]
    return _digest(tagged(value))


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("绑定必须为有限 JSON") from None
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _object(raw: str, schema: dict) -> dict:
    def pairs(items: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON 含重复键")
            result[key] = value
        return result

    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError("参数超过 64 KiB")
        value = json.loads(raw, object_pairs_hook=pairs, parse_int=_Integer, parse_float=_Float,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限数")))
        if not isinstance(value, dict):
            raise ValueError("参数必须为 JSON 对象")
        _digest(value)
        Draft202012Validator(schema).validate(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("无效工具参数") from None
    except Exception as exc:
        from jsonschema.exceptions import ValidationError
        if isinstance(exc, ValidationError):
            raise ValueError("工具参数不符合 schema") from None
        raise


def _selection_only(schema: dict) -> bool:
    # 字段名和结构均须在已知选择器集合；这不构成工具安全判断。
    names = {"id", "item_id", "path", "file_path", "input_path", "source_path", "cursor"}
    integers = {"limit", "offset", "page"}
    properties = schema.get("properties")
    if (schema.get("type") != "object" or schema.get("additionalProperties") is not False
            or not isinstance(properties, dict) or not properties or set(properties) - names - integers - {"ids"}
            or set(schema) - {"type", "properties", "required", "additionalProperties", "description", "title"}):
        return False
    if "ids" in properties:
        ids = properties["ids"]
        if (not set(properties).intersection({"path", "file_path", "input_path", "source_path"})
                or not isinstance(ids, dict) or ids.get("type") != "array"
                or set(ids) - {"type", "items", "description", "title", "minItems", "maxItems"}
                or not isinstance(ids.get("items"), dict) or ids["items"].get("type") != "string"
                or set(ids["items"]) - {"type", "description", "title", "minLength", "maxLength", "enum"}):
            return False
    return all(isinstance(value, dict) and value.get("type") == ("integer" if key in integers else "string")
               and not (set(value) - {"type", "description", "title", "default", "enum", "const", "minimum", "maximum", "minLength", "maxLength"})
               for key, value in properties.items() if key != "ids")


class FlowActionPreflight:
    """实例仅用于一个请求；调用者负责审批、审计及原有权限检查。"""

    def __init__(self, definitions: list[dict], *, occupied_names=(), max_pending: int = 64, stage_selectors: bool = False):
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("草稿容量无效")
        self._max_pending = max_pending
        self._definitions = copy.deepcopy(definitions)
        self._by_name = {}
        for definition in self._definitions:
            name = definition["name"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name) or name in self._by_name:
                raise ValueError("工具名称无效或重复")
            Draft202012Validator.check_schema(definition["parameters"])
            _digest(definition)
            self._by_name[name] = definition
        used = set(occupied_names) | set(self._by_name)
        self._previews = {}
        for name, definition in self._by_name.items():
            if not stage_selectors and _selection_only(definition["parameters"]):
                continue
            base = "draft_" + name[:58]
            candidate = base
            suffix = 0
            while candidate in used:
                suffix += 1
                tail = "_" + str(suffix)
                candidate = base[:64 - len(tail)] + tail
            used.add(candidate)
            self._previews[candidate] = name
        self._records: dict[str, tuple[str, str, str, str, str]] = {}
        self._consumed: deque[tuple[str, str, str, str]] = deque(maxlen=max(256, max_pending))

    def original_for_preview(self, name: str) -> str | None:
        return self._previews.get(name)

    def preview_for_original(self, name: str) -> str | None:
        return next((preview for preview, original in self._previews.items() if original == name), None)

    def schemas(self) -> list[dict]:
        result = []
        for name, definition in self._by_name.items():
            preview = self.preview_for_original(name)
            approved = any(row[0] == name and row[4] == "approved" for row in self._records.values())
            description = definition["description"]
            if not preview or approved:
                formal = "正式执行已批准的草稿，调用后返回业务结果。 " + description if approved else description
                result.append({"type": "function", "function": {"name": name, "description": formal,
                                                                   "parameters": copy.deepcopy(definition["parameters"])}})
            if preview:
                result.append({"type": "function", "function": {"name": preview,
                    "description": f"预审 {name} 的新草稿或修订，不执行业务。获批后用原名 {name} 和相同参数正式提交。 {description}",
                    "parameters": copy.deepcopy(definition["parameters"])}})
        return result

    def draft(self, preview_name: str, arguments_json: str, binding: object) -> str:
        name = self._previews.get(preview_name)
        if name is None:
            raise ValueError("未知草稿工具")
        definition = self._by_name[name]
        signature = (name, _argument_digest(_object(arguments_json, definition["parameters"])), _digest(binding), _digest(definition))
        if signature in self._consumed or any(row[:4] == signature for row in self._records.values()):
            raise ValueError("相同草稿已经提交或消费")
        if len(self._records) >= self._max_pending:
            raise ValueError("草稿容量已满")
        identifier = secrets.token_urlsafe(24)
        self._records[identifier] = (*signature, "pending")
        return identifier

    def approve(self, draft_id: str, binding: object) -> bool:
        row = self._records.get(draft_id)
        if row is None or row[4] != "pending" or row[2] != _digest(binding):
            return False
        self._records[draft_id] = (*row[:4], "approved")
        return True

    def approved_ids(self) -> frozenset[str]:
        """调用模型之前捕获已获批凭据，不能从同一响应中追加。"""
        return frozenset(identifier for identifier, row in self._records.items() if row[4] == "approved")

    def reserve_approval(self, name: str, arguments_json: str, binding: object, definition: dict,
                         *, issued: frozenset[str]) -> str | None:
        if not isinstance(issued, frozenset):
            return None
        if name not in self._by_name:
            return None
        try:
            signature = (name, _argument_digest(_object(arguments_json, self._by_name[name]["parameters"])),
                         _digest(binding), _digest(definition))
        except ValueError:
            return None
        for identifier, row in list(self._records.items()):
            if identifier in issued and row[:4] == signature and row[4] == "approved":
                self._records[identifier] = (*row[:4], "reserved")
                return identifier
        return None

    def consume_reservation(self, draft_id: str) -> bool:
        row = self._records.get(draft_id)
        if row is None or row[4] != "reserved":
            return False
        del self._records[draft_id]
        self._consumed.append(row[:4])
        return True

    def take_approval(self, name: str, arguments_json: str, binding: object, definition: dict,
                      *, issued: frozenset[str]) -> str | None:
        identifier = self.reserve_approval(name, arguments_json, binding, definition, issued=issued)
        if identifier is not None and self.consume_reservation(identifier):
            return identifier
        return None

    def consume(self, name: str, arguments_json: str, binding: object, definition: dict) -> bool:
        return self.take_approval(name, arguments_json, binding, definition, issued=self.approved_ids()) is not None

    def discard(self, draft_id: str) -> bool:
        row = self._records.get(draft_id)
        if row is None or row[4] == "consumed":
            return False
        del self._records[draft_id]
        return True
