"""将有明确归属的结构化事项投影为独立复核输入。"""
import json
import re

from agent_core.core.flow_draft_review import _ASSERTION, _CUSTOMER, _MANDATE, _RULE, needs_review

_MAX_TEXT = 500_000
_MAX_ITEMS = 32
_MAX_UNITS = 12
_META = {"source_path"}
_CROSS = re.compile(r"同上|上述|下述|该事项|该工单|其他事项|其它事项|其余事项|另(?:一|个)事项|两者|各事项|所有事项|"
                    r"above|below|other items?|previous items?|both items?|all items?", re.I)


class _DuplicateKey(ValueError):
    pass


def _pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate key")
        result[key] = value
    return result


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def _parse(text: str) -> object:
    text = text.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, re.I | re.S)
        if not match:
            raise ValueError("invalid fence")
        text = match.group(1)
    return json.loads(text, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def _records(data: object) -> list[tuple[str | None, dict]]:
    if isinstance(data, list):
        groups = [(None, data)]
    elif isinstance(data, dict):
        groups = []
        for key, value in data.items():
            if needs_review(key) or _CROSS.search(key):
                raise ValueError("global assertion")
            if isinstance(value, str):
                continue
            if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                continue
            if not isinstance(value, list):
                raise ValueError("unknown root content")
            groups.append((key, value))
    else:
        raise ValueError("unknown root")
    records = []
    seen = set()
    for group, rows in groups:
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                    or len(row["id"]) > 128 or row["id"] in seen
                    or any(not _record_value(v) for v in row.values())):
                raise ValueError("invalid record")
            seen.add(row["id"])
            records.append((group, row))
            if len(records) > _MAX_ITEMS:
                raise ValueError("too many items")
    return records


def _record_value(value: object) -> bool:
    return (isinstance(value, (str, int, float, bool, type(None))) or
            isinstance(value, list) and len(value) <= 32 and all(isinstance(item, str) for item in value))


def _source_array(text: str, data: object) -> list[tuple[dict, str]] | None:
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


def _references(records: list[dict]) -> bool:
    ids = {row["id"] for row in records}
    pattern = _id_pattern(ids)
    for row in records:
        for key, value in row.items():
            if key == "id":
                continue
            for text in value if isinstance(value, list) else [value]:
                if isinstance(text, str) and (_CROSS.search(text) or any(match.group() != row["id"] for match in pattern.finditer(text))):
                    return True
    return False


def _id_pattern(ids: set[str]) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True)) + r")(?![A-Za-z0-9_])")


def _shared(data: object, records: list[tuple[str | None, dict]]) -> tuple[dict, set[str]]:
    if not isinstance(data, dict):
        return {}, set()
    ids = [row["id"] for _, row in records]
    pattern = _id_pattern(set(ids))
    metadata = {}
    owners = set()
    for key, value in data.items():
        if isinstance(value, list) and (not value or all(isinstance(item, dict) for item in value)):
            continue
        if isinstance(value, list):
            if value != ids:
                raise ValueError("unmatched shared ids")
        elif isinstance(value, str):
            if len(value) > 32_000 or _CROSS.search(value):
                raise ValueError("ambiguous shared text")
            for sentence in re.split(r"[。！？；\n.!?;]", value):
                if needs_review(sentence):
                    matches = set(pattern.findall(sentence))
                    if len(matches) != 1:
                        raise ValueError("unowned shared assertion")
                    owners.update(matches)
        else:
            raise ValueError("unknown metadata")
        metadata[key] = value
    return metadata, owners


def review_units(draft: str, sources: list[dict], *, required_ids: frozenset[str] = frozenset()) -> list[dict] | None:
    """不能确定独立归属或覆盖时返回 None，调用方保留整稿复核。"""
    if not isinstance(draft, str) or len(draft) > _MAX_TEXT or not isinstance(sources, list):
        return None
    try:
        data = _parse(draft)
        records = _records(data)
        ids = {row["id"] for _, row in records}
        if len(records) < 2 or not isinstance(required_ids, frozenset) or not required_ids <= ids:
            return None
        if _references([row for _, row in records]) or len(sources) > 64:
            return None
        metadata, owners = _shared(data, records)
        seen = set()
        total = 0
        indexed = []
        trusted = {}
        for source in sources:
            if (not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"]
                    or source["id"] in seen or not isinstance(source.get("text"), str)):
                return None
            seen.add(source["id"])
            text = source["text"]
            total += len(text)
            if total > _MAX_TEXT:
                return None
            fragments = None
            try:
                source_data = _parse(text)
                fragments = _source_array(text, source_data)
                if fragments is None and source.get("kind") != "rag_search" and isinstance(source_data, dict):
                    for key in ("items", "records"):
                        rows = source_data.get(key)
                        if isinstance(rows, list) and any(isinstance(item, dict) and item.get("id") in ids for item in rows):
                            return None
            except _DuplicateKey:
                return None
            except (ValueError, TypeError, IndexError, RecursionError):
                # 无法解析的来源原样保留；不作为复制的证明。
                fragments = None
            mapping = {}
            if fragments is not None:
                for item, fragment in fragments:
                    identifier = item.get("id")
                    if (not isinstance(identifier, str) or not identifier or identifier in mapping
                            or any(not _record_value(value) for value in item.values())):
                        return None
                    mapping[identifier] = (item, fragment)
                if len(mapping) > _MAX_ITEMS or _references([item for item, _ in fragments]):
                    return None
                if (source.get("kind"), source.get("scope")) in {("user_input", "user_report"), ("custom_result", "tool_report")}:
                    for identifier, (item, _) in mapping.items():
                        if isinstance(item.get("text"), str):
                            trusted.setdefault(identifier, []).append(item["text"])
            indexed.append((source, mapping))
        if any(len(set(values)) > 1 for values in trusted.values()):
            return None
        units = []
        output_chars = 0
        for group, row in records:
            identifier = row["id"]
            text = row.get("text")
            copies = trusted.get(identifier, [])
            copied = isinstance(text, str) and len(copies) == 1 and copies[0] == text
            if copied and re.search(_RULE, text, re.I) and re.search(_MANDATE, text, re.I):
                copied = False
            other = json.dumps({key: value for key, value in row.items() if key not in {"id", "text"}}, ensure_ascii=False)
            # 原话与其他字段共同构成同一行断言时仍须复核。
            cross = copied and ((re.search(_CUSTOMER, text, re.I) and re.search(_ASSERTION, other, re.I))
                                or (re.search(_ASSERTION, text, re.I) and re.search(_CUSTOMER, other, re.I))
                                or (re.search(_RULE, text, re.I) and re.search(_MANDATE, other, re.I))
                                or (re.search(_MANDATE, text, re.I) and re.search(_RULE, other, re.I)))
            filtered = {key: value for key, value in row.items() if not (key == "text" and copied)}
            if identifier not in required_ids and identifier not in owners and not cross and not needs_review(json.dumps(filtered, ensure_ascii=False)):
                continue
            projected = []
            for source, mapping in indexed:
                if source.get("kind") == "rag_search":
                    projected.append(source.copy())
                elif identifier in mapping:
                    projected.append({**source, "text": mapping[identifier][1]})
                elif not (set(mapping) & ids):
                    projected.append(source.copy())
            if group is None:
                item = [row]
            else:
                item = {key: ([row] if key == group else metadata[key])
                        for key in data if key == group or key in metadata}
            unit_draft = json.dumps(item, ensure_ascii=False)
            output_chars += len(unit_draft) + sum(len(source["text"]) for source in projected)
            if len(units) >= _MAX_UNITS or output_chars > 2_000_000:
                return None
            units.append({"item_id": identifier, "draft": unit_draft, "sources": projected})
        return units
    except (ValueError, TypeError, IndexError, RecursionError, OverflowError):
        return None
