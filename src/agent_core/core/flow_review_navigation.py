"""将整稿中的待核原句定位到已观察的事项编号，仅用于来源导航。"""
import re
from collections import Counter

from agent_core.core.flow_review_records import RECORD_ID_KEYS, parse_record_json, record_identifier


def _draft_records(document: object) -> tuple[Counter, set[int]] | None:
    counts = Counter()
    records: set[int] = set()
    stack = [(document, ())]
    visited = 0
    while stack:
        value, parents = stack.pop()
        visited += 1
        if visited > 2048 or len(parents) > 8:
            return None
        if isinstance(value, dict):
            if RECORD_ID_KEYS.intersection(value):
                # 含有下一层编号记录的包装对象，不给共享说明标上自己的编号。
                records.difference_update(parents)
                identifier = record_identifier(value)
                if identifier:
                    records.add(id(value))
                aliases = {value[key] for key in RECORD_ID_KEYS if isinstance(value.get(key), str)}
                for alias in aliases:
                    counts[alias] += 1 if identifier else 2
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        if visited + len(stack) + len(children) > 2048:
            return None
        stack.extend((child, (*parents, id(value))) for child in children)
    return counts, records


def _locate_target(document: object, target: dict, records: set[int]) -> tuple[str | None, str] | None:
    path = target.get("field")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
        return None
    value = document
    owner = None
    for token in path[1:].split("/"):
        if re.search(r"~(?![01])", token):
            return None
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            if RECORD_ID_KEYS.intersection(value):
                owner = record_identifier(value) if id(value) in records else None
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]{0,5}", key) and int(key) < len(value):
            value = value[int(key)]
            if isinstance(value, dict):
                owner = None
        else:
            return None
    start, end, text = (target.get(k) for k in ("start", "end", "text"))
    if (not isinstance(value, str) or type(start) is not int or type(end) is not int
            or not isinstance(text, str) or not 0 < len(text) <= 4000 or not 0 <= start < end <= len(value)
            or value[start:end] != text):
        return None
    return owner, text


def target_record_references(
    draft: str, targets: list[dict], known: set[str],
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """返回字段所属记录和原句点名的编号；两者均不构成事实证明。"""
    if (not isinstance(draft, str) or len(draft) > 500_000 or not 1 <= len(known) <= 256
            or not isinstance(targets, list) or not 1 <= len(targets) <= 64):
        return {}, {}
    try:
        document = parse_record_json(draft)
    except (ValueError, TypeError, RecursionError):
        return {}, {}
    if not isinstance(document, (dict, list)):
        return {}, {}
    index = _draft_records(document)
    ids = [row.get("id") if isinstance(row, dict) else None for row in targets]
    if index is None or not all(isinstance(key, str) and key for key in ids) or len(set(ids)) != len(ids):
        return {}, {}
    counts, records = index
    # 字段中的精确标识可用于任何合法编号。正文只识别边界清楚的完整编号。
    literals = sorted((k for k in known if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{1,127}", k)),
                      key=lambda k: (-len(k), k))
    pattern = (re.compile(r"(?<![A-Za-z0-9_.-])(?:" + "|".join(re.escape(k) for k in literals)
                          + r")(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])") if literals else None)
    owners, references = {}, {}
    for row in targets:
        located = _locate_target(document, row, records)
        if located is None:
            continue
        owner, text = located
        refs = []
        if owner in known and counts[owner] == 1:
            owners[row["id"]] = owner
            refs.append(owner)
        if pattern:
            refs.extend(match.group() for match in pattern.finditer(text) if counts[match.group()] <= 1)
        unique = tuple(dict.fromkeys(refs))
        if 0 < len(unique) <= 8:
            references[row["id"]] = unique
    return owners, references
