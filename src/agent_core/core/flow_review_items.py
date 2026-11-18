"""将有明确归属的结构化事项投影为独立复核输入。"""
import hashlib
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


def tool_review_sources(draft: str, sources: list[dict]) -> list[dict]:
    """仅凭来源数组中的唯一标识定位原文；无法确认时原样返回。"""
    if not isinstance(draft, str) or len(draft) > _MAX_TEXT or not isinstance(sources, list) or _CROSS.search(draft):
        return sources
    try:
        arrays = []
        observed = set()
        observed_rows = {}
        keys = ("id", "feedback_id", "item_ref", "item_id", "record_id", "ticket_id")
        for index, source in enumerate(sources):
            if (not isinstance(source, dict) or not isinstance(source.get("text"), str)
                    or (source.get("kind"), source.get("scope")) not in
                    {("custom_result", "tool_report"), ("user_input", "user_report")}):
                continue
            text = source["text"]
            try:
                fragments = _source_array(text, _parse(text))
            except (ValueError, TypeError, IndexError, RecursionError):
                continue
            if not fragments:
                continue
            mapping = {}
            for row, fragment in fragments:
                values = [row[key] for key in keys if key in row]
                if (not values or any(not isinstance(v, str) or not v or len(v) > 128 for v in values)
                        or len(set(values)) != 1 or values[0] in mapping
                        or any(not _record_value(value) for value in row.values())):
                    return sources
                if values[0] in observed_rows and observed_rows[values[0]] != row:
                    return sources
                observed_rows[values[0]] = row
                mapping[values[0]] = (row, fragment)
            if len(mapping) > _MAX_ITEMS:
                return sources
            observed.update(mapping)
            arrays.append((index, mapping))
        if not observed:
            return sources
        pattern = _id_pattern(observed)
        matches = set(pattern.findall(draft))
        if len(matches) != 1:
            return sources
        selected = next(iter(matches))
        for _, mapping in arrays:
            for identifier, (row, _) in mapping.items():
                for key, value in row.items():
                    if key in keys:
                        continue
                    for text in value if isinstance(value, list) else [value]:
                        if isinstance(text, str) and (_CROSS.search(text) or any(
                                match.group() != identifier for match in pattern.finditer(text))):
                            return sources
        result = list(sources)
        for index, mapping in arrays:
            if selected in mapping:
                result[index] = {**sources[index], "text": mapping[selected][1]}
        return result
    except (ValueError, TypeError, IndexError, RecursionError, OverflowError):
        return sources


def _dependent_shared_context(value: str, disputed: list[str]) -> bool:
    """仅允许争议句内前半句已提供先行内容的指代。"""
    pronoun = re.compile(r"这|那|此|其(?!他|它|余)|(?<!其)它|上述|以下|前述|该|\b(?:these|those|it|they|their)\b", re.I)
    sentences = re.findall(r"[^。！？；\n.!?;]+[。！？；\n.!?;]?", value)
    for index, sentence in enumerate(sentences):
        if sentence not in disputed:
            continue
        for match in pronoun.finditer(sentence):
            before = sentence[:match.start()]
            if not re.search(r"[,，]", before) or not needs_review(before):
                return True
        if (index + 1 < len(sentences) and
                re.match(r"\s*(?:这|那|此|其|它|该|these\b|those\b|it\b|they\b|their\b)", sentences[index + 1], re.I)):
            return True
    return False


def _shared(data: object, records: list[tuple[str | None, dict]], required: frozenset[str]) -> tuple[dict, set[str], list[dict]]:
    if not isinstance(data, dict):
        return {}, set(), []
    ids = [row["id"] for _, row in records]
    pattern = _id_pattern(set(ids))
    metadata = {}
    owners = set()
    claims = []
    for key, value in data.items():
        if isinstance(value, list) and (not value or all(isinstance(item, dict) for item in value)):
            continue
        if isinstance(value, list):
            if value != ids:
                raise ValueError("unmatched shared ids")
        elif isinstance(value, str):
            if len(value) > 32_000 or _CROSS.search(value):
                raise ValueError("ambiguous shared text")
            unit_id = "shared_claim:" + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
            if unit_id in ids:
                raise ValueError("shared identifier collision")
            disputed = []
            for sentence in re.findall(r"[^。！？；\n.!?;]+[。！？；\n.!?;]?", value):
                if needs_review(sentence):
                    matches = set(pattern.findall(sentence))
                    if len(matches) == 1:
                        owners.update(matches)
                    elif len(matches) > 1:
                        raise ValueError("multiple shared owners")
                    elif (not (re.search(_RULE, sentence, re.I) and re.search(_MANDATE, sentence, re.I)
                                  or re.match(r"\s*(?:我|我们|本人|本方|I\b|we\b)", sentence, re.I))):
                        raise ValueError("ambiguous unowned assertion")
                    else:
                        disputed.append(sentence)
            if disputed or unit_id in required:
                if _dependent_shared_context(value, disputed):
                    raise ValueError("shared context may depend on another statement")
                claims.append({"item_id": unit_id, "kind": "shared_claim", "draft": json.dumps(
                    {"field": key, "claims": disputed or [value], "context": value, "item_ids": ids}, ensure_ascii=False)})
        else:
            raise ValueError("unknown metadata")
        metadata[key] = value
    return metadata, owners, claims


def review_units(draft: str, sources: list[dict], *, required_ids: frozenset[str] = frozenset()) -> list[dict] | None:
    """不能确定独立归属或覆盖时返回 None，调用方保留整稿复核。"""
    if not isinstance(draft, str) or len(draft) > _MAX_TEXT or not isinstance(sources, list):
        return None
    try:
        data = _parse(draft)
        records = _records(data)
        ids = {row["id"] for _, row in records}
        if len(records) < 2 or not isinstance(required_ids, frozenset):
            return None
        if _references([row for _, row in records]) or len(sources) > 64:
            return None
        metadata, owners, shared_claims = _shared(data, records, required_ids)
        item_metadata = metadata.copy()
        for shared in shared_claims:
            payload = json.loads(shared["draft"])
            # 单项草稿保留其他共享内容；无主争议只由共享单元裁决。
            if needs_review("".join(payload["claims"])):
                item_metadata[payload["field"]] = "".join(
                    sentence for sentence in re.findall(r"[^。！？；\n.!?;]+[。！？；\n.!?;]?", payload["context"])
                    if sentence not in payload["claims"])
        if not required_ids <= ids | {unit["item_id"] for unit in shared_claims}:
            return None
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
                item = {key: ([row] if key == group else item_metadata[key])
                        for key in data if key == group or key in metadata}
            unit_draft = json.dumps(item, ensure_ascii=False)
            output_chars += len(unit_draft) + sum(len(source["text"]) for source in projected)
            if len(units) >= _MAX_UNITS or output_chars > 2_000_000:
                return None
            units.append({"item_id": identifier, "draft": unit_draft, "sources": projected})
        for unit in shared_claims:
            unit["sources"] = [source.copy() for source in sources]
            output_chars += len(unit["draft"]) + sum(len(source["text"]) for source in sources)
            if len(units) >= _MAX_UNITS or output_chars > 2_000_000:
                return None
            units.append(unit)
        return units
    except (ValueError, TypeError, IndexError, RecursionError, OverflowError):
        return None
