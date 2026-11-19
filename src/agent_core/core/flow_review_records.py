"""解析复核来源的结构化记录并定位连续原文。"""
import json
import re


class DuplicateRecordKey(ValueError):
    pass


def _pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateRecordKey("duplicate key")
        result[key] = value
    return result


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def parse_record_json(text: str) -> object:
    text = text.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, re.I | re.S)
        if not match:
            raise ValueError("invalid fence")
        text = match.group(1)
    return json.loads(text, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def source_array_records(text: str, data: object) -> list[tuple[dict, str]] | None:
    """只接受简单来源包装；切片必须经原解码器重新核对。"""
    if isinstance(data, dict):
        keys = set(data) & {"items", "records"}
        if (len(keys) != 1 or set(data) - {"items", "records", "ok", "path", "total", "count", "skipped"}
                or ("ok" in data and data["ok"] is not True)
                or ("path" in data and not isinstance(data["path"], str))
                or any(type(data[k]) is not int or data[k] < 0 for k in ("total", "count", "skipped") if k in data)):
            return None
        array_key = next(iter(keys))
        target = data[array_key]
        if (not isinstance(target, list) or ("count" in data and data["count"] != len(target))
                or ("total" in data and data["total"] < len(target) + data.get("skipped", 0))):
            raise ValueError("contradictory source metadata")
        pos = text.find("{") + 1
        while True:
            pos = _space(text, pos)
            key, pos = _DECODER.raw_decode(text, pos)
            pos = _space(text, pos)
            if text[pos] != ":":
                raise ValueError("invalid object")
            pos = _space(text, pos + 1)
            if key == array_key:
                break
            _, pos = _DECODER.raw_decode(text, pos)
            pos = _space(text, pos)
            if text[pos] != ",":
                raise ValueError("missing items")
            pos += 1
    elif isinstance(data, list):
        target = data
        pos = _space(text, 0)
    else:
        return None
    if text[pos] != "[":
        raise ValueError("invalid array")
    pos += 1
    result = []
    for expected in target:
        pos = _space(text, pos)
        start = pos
        actual, pos = _DECODER.raw_decode(text, pos)
        if actual != expected or not isinstance(actual, dict):
            raise ValueError("invalid item")
        result.append((actual, text[start:pos]))
        pos = _space(text, pos)
        if len(result) < len(target):
            if text[pos] != ",":
                raise ValueError("invalid array")
            pos += 1
    if text[_space(text, pos)] != "]":
        raise ValueError("invalid array")
    return result


def _space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos
