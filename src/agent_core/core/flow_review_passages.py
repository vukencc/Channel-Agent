"""为已观察来源生成有界、可复算的原文引用；不扩大来源权限。"""
import json
import re
from collections import deque
from bisect import bisect_left

from agent_core.core.flow_review_records import parse_record_json, source_array_records

MAX_PASSAGES = 64
MAX_PASSAGE_CHARS = 8192
MAX_QUOTE = 160
_SCOPES = {("read_file", "observed_span"), ("rag_search", "observed_span"),
           ("user_input", "user_report"), ("custom_result", "tool_report")}
_KEY_SEPARATOR = re.compile(r"\s*:")
_ID_KEYS = {"id", "feedback_id", "item_ref", "item_id", "record_id", "ticket_id"}


def _unique_pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _sentences(text: str, offset: int):
    # 不在分号、逗号处断开，避免丢失同一句的条件或例外。
    for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text):
        raw = match.group()
        value = raw.strip()
        if value and len(value) <= MAX_QUOTE:
            start = offset + match.start() + len(raw) - len(raw.lstrip())
            yield start, start + len(value), value


def _spans(text: str):
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(text, object_pairs_hook=_unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            return
        # 仅索引无转义的字符串值，保证切片本身就是解码后的原文。
        for match in re.finditer(r'"(?:[^"\\]|\\.)*"', text):
            token = match.group()
            if _KEY_SEPARATOR.match(text, match.end()) or "\\" in token:
                continue
            yield from _sentences(token[1:-1], match.start() + 1)
    else:
        yield from _sentences(text, 0)


def _line_records(source: dict) -> list[tuple[dict, int, int]] | None:
    if ((source.get("kind"), source.get("scope")) != ("read_file", "observed_span")
            or not isinstance(source.get("path"), str)
            or not source["path"].lower().endswith((".jsonl", ".ndjson"))):
        return None
    text = source["text"]
    try:
        parse_record_json(text)
        return None
    except (ValueError, RecursionError):
        pass
    records = []
    identities = set()
    offset = 0
    try:
        for line in text.split("\n"):
            content = line[:-1] if line.endswith("\r") else line
            if "\r" in content:
                return []
            raw = content.strip(" \t")
            if raw:
                if not raw.startswith("{") or not raw.endswith("}"):
                    return []
                value = parse_record_json(raw)
                if not isinstance(value, dict):
                    return []
                identity = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
                if identity in identities:
                    return []
                identities.add(identity)
                start = offset + len(line) - len(line.lstrip(" \t"))
                records.append((value, start, start + len(raw)))
            offset += len(line) + 1
    except (ValueError, TypeError, RecursionError):
        return []
    return records if len(records) >= 2 else []


def _round_robin(iterators):
    pending = deque(iter(iterator) for iterator in iterators)
    while pending:
        iterator = pending.popleft()
        try:
            value = next(iterator)
        except StopIteration:
            continue
        yield value
        pending.append(iterator)


def _record_spans(text: str, start: int, end: int):
    for a, b, value in _spans(text[start:end]):
        yield start + a, start + b, value


def _fair_record_spans(text: str, records: list[tuple[dict, int, int]]):
    pending = deque()
    remaining = iter(records)
    while True:
        while len(pending) < MAX_PASSAGES:
            try:
                _, start, end = next(remaining)
            except StopIteration:
                break
            iterator = iter(_record_spans(text, start, end))
            first = next(iterator, None)
            if first is not None:
                pending.append((first, iterator))
        if not pending:
            return
        value, iterator = pending.popleft()
        yield value
        following = next(iterator, None)
        if following is not None:
            pending.append((following, iterator))


def review_passages(sources: list[dict]) -> list[dict]:
    """返回有限提示，不声称覆盖来源；重复或无效来源返回空列表。"""
    if not isinstance(sources, list) or len(sources) > 128:
        return []
    seen = set()
    source_chars = 0
    for row in sources:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"].strip() or len(row["id"]) > 120 or row["id"] in seen
                or not isinstance(row.get("text"), str) or not row["text"]):
            return []
        seen.add(row["id"])
        source_chars += len(row["text"])
        if source_chars > 1_000_000:
            return []
    result = []
    total = 0
    eligible = [row for row in sources if (row.get("kind"), row.get("scope")) in _SCOPES]
    line_records = {row["id"]: _line_records(row) for row in eligible}
    fair = len(eligible) > 1 and any(records for records in line_records.values())

    def source_spans(row):
        records = line_records[row["id"]]
        if records is None:
            spans = _spans(row["text"])
        elif fair:
            spans = _fair_record_spans(row["text"], records)
        else:
            spans = (span for _, start, end in records
                     for span in _record_spans(row["text"], start, end))
        for start, end, value in spans:
            yield row["id"], start, end, value

    streams = [source_spans(row) for row in eligible]
    ordered = _round_robin(streams) if fair else (span for stream in streams for span in stream)
    for source_id, start, end, value in ordered:
        if len(result) >= MAX_PASSAGES or total + len(value) > MAX_PASSAGE_CHARS:
            return result
        result.append({"id": f"E{len(result) + 1}", "source_id": source_id,
                       "start": start, "end": end, "text": value})
        total += len(value)
    return result


def _terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words


def _draft_id(draft: str) -> str | None:
    if not isinstance(draft, str) or len(draft) > 500_000:
        return None
    try:
        data = json.loads(draft, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        stack = [data]
        ids = set()
        count = 0
        while stack:
            value = stack.pop()
            count += 1
            if count > 2048:
                return None
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in _ID_KEYS and isinstance(child, str) and child.strip() and len(child) <= 128:
                        ids.add(child)
                    elif isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
        return next(iter(ids)) if len(ids) == 1 else None
    except (ValueError, RecursionError):
        return None


def relevant_passages(sources: list[dict], targets: list[dict], draft: str) -> list[dict]:
    """选择相关的有限提示；索引并非证据裁决，也不代表来源已覆盖。"""
    passages = review_passages(sources)
    if not passages or not isinstance(targets, list):
        return []
    query = _terms(" ".join(t.get("text", "") for t in targets
                            if isinstance(t, dict) and isinstance(t.get("text"), str))[:256_000])
    identifier = _draft_id(draft)
    by_source = {source["id"]: source for source in sources}
    ranges = {}
    if identifier:
        indexed_sources = {part["source_id"] for part in passages}
        for source in sources:
            if source["id"] not in indexed_sources:
                continue
            try:
                records = _line_records(source)
                if records is not None:
                    starts = sorted(p["start"] for p in passages if p["source_id"] == source["id"])
                    spans = []
                    for row, start, end in records:
                        index = bisect_left(starts, start)
                        if index == len(starts) or starts[index] >= end:
                            continue
                        ids = {row[k] for k in _ID_KEYS if isinstance(row.get(k), str) and row[k].strip()}
                        matched = next(iter(ids)) == identifier if len(ids) == 1 else None
                        spans.append((start, end, matched))
                    ranges[source["id"]] = spans
                    continue
                data = parse_record_json(source["text"])
                rows = data.get("items", data.get("records")) if isinstance(data, dict) else data
                if not isinstance(rows, list) or len(rows) > MAX_PASSAGES:
                    continue
                fragments = source_array_records(source["text"], data)
                if fragments is None:
                    continue
                spans = []
                for row, raw in fragments:
                    if source["text"].count(raw) != 1:
                        continue
                    start = source["text"].index(raw)
                    matched = any(row.get(k) == identifier for k in _ID_KEYS)
                    spans.append((start, start + len(raw), matched))
                ranges[source["id"]] = spans
            except (ValueError, TypeError, RecursionError, IndexError):
                continue
    ranked = []
    for order, passage in enumerate(passages):
        source = by_source[passage["source_id"]]
        span_match = next((matched for start, end, matched in ranges.get(source["id"], [])
                           if start <= passage["start"] and passage["end"] <= end), None)
        if span_match is False:
            continue
        same_item = span_match is True or (identifier is not None and source.get("item_ref") == identifier)
        score = len(query & _terms(passage["text"]))
        if same_item or score:
            ranked.append((passage, same_item, score, order))
    # 唯一的原句即使没有词面重叠，也可用于核对同义转述或矛盾。
    if not ranked and len(passages) == 1 and identifier is None:
        return passages
    ranked.sort(key=lambda row: (-int(row[1]), -row[2], row[3]))
    # 各相关来源先各保留一条，避免单一来源挤掉冲突来源。
    first = []
    seen_sources = set()
    for row in ranked:
        if row[0]["source_id"] not in seen_sources:
            first.append(row)
            seen_sources.add(row[0]["source_id"])
    ordered = first + [row for row in ranked if row not in first]
    result = []
    chars = 0
    for passage, _, _, _ in ordered:
        if len(result) >= 12:
            break
        if chars + len(passage["text"]) <= 1200:
            result.append(passage)
            chars += len(passage["text"])
    return result


def resolve_passage_issues(verdict: dict, sources: list[dict], targets: list[dict],
                           passages: list[dict]) -> dict | None:
    """仅转换实际签发的索引子集中的 revise；无效或不适用时返回 None。"""
    if (not isinstance(verdict, dict) or set(verdict) != {"status", "issues"}
            or verdict.get("status") != "revise" or not isinstance(verdict.get("issues"), list)
            or not 1 <= len(verdict["issues"]) <= 5 or not isinstance(passages, list)
            or not passages
            or not isinstance(targets, list) or not targets):
        return None
    full = {p["id"]: p for p in review_passages(sources)}
    seen = set()
    for passage in passages:
        if (not isinstance(passage, dict) or not isinstance(passage.get("id"), str)
                or passage["id"] in seen or full.get(passage["id"]) != passage):
            return None
        seen.add(passage["id"])
    target_index = {}
    for target in targets:
        if (not isinstance(target, dict) or not isinstance(target.get("id"), str)
                or not target["id"].strip() or len(target["id"]) > 120
                or target["id"] in target_index or not isinstance(target.get("text"), str)
                or not target["text"].strip() or len(target["text"]) > 4000):
            return None
        target_index[target["id"]] = target["text"]
    evidence = {p["id"]: p for p in passages}
    issues = []
    for issue in verdict["issues"]:
        if not isinstance(issue, dict) or set(issue) != {"target_id", "evidence_id", "reason"}:
            return None
        target_id, evidence_id, reason = (issue[k] for k in ("target_id", "evidence_id", "reason"))
        if (not all(isinstance(v, str) and v.strip() for v in (target_id, evidence_id, reason))
                or len(reason) > 300 or target_id not in target_index or evidence_id not in evidence):
            return None
        passage = evidence[evidence_id]
        issues.append({"target_id": target_id, "draft_quote": target_index[target_id],
                       "source_id": passage["source_id"], "quote": passage["text"], "reason": reason})
    return {"status": "revise", "issues": issues}
