"""从本轮已执行的只读工具结果中收集可核验的原文片段。"""
from collections import Counter
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re

from agent_core.core.instrumentation import tool_outcome
from agent_core.core.messages import is_user_request
from agent_core.core.tool_observations import validate_observation



def _custom_names(record: dict) -> dict[str, dict]:
    # 父会话授权不能从本地快照独立确认，故只接受直接节点会话。
    branch = record.get("branch") or {}
    if (not isinstance(branch, dict) or not record.get("flow_id") or not record.get("flow_node_id")
            or record.get("delegated_from") or branch.get("parent_id")):
        return {}
    from agent_core.core.custom_tools import validate_custom_tools
    try:
        definitions = validate_custom_tools(record.get("custom_tools"))
        if record.get("inherited_custom_tools") is not None:
            inherited = {item["name"]: item for item in validate_custom_tools(record["inherited_custom_tools"])}
            definitions = [item for item in definitions if inherited.get(item["name"]) == item]
    except (ValueError, TypeError, KeyError):
        return {}
    allowed = {item["name"] for item in definitions}
    for key in ("tool_names", "inherited_tool_names"):
        names = record.get(key)
        if names is not None:
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                return {}
            allowed.intersection_update(names)
    return {item["name"]: item for item in definitions if item["name"] in allowed}


def _custom_args(arguments: object, definition: dict) -> tuple[str | None, str | None] | None:
    if not isinstance(arguments, dict):
        return None
    names = {"path", "file_path", "input_path", "source_path"}
    path_keys = names & arguments.keys()
    selectors = {"id", "ids", "item_id", "item_ids", "limit", "offset", "page", "cursor"}
    if len(path_keys) > 1 or set(arguments) - path_keys - selectors:
        return None
    if path_keys:
        path = arguments[next(iter(path_keys))]
        path_kind = "path"
    else:
        properties = definition["parameters"].get("properties", {})
        defaults = [properties[name].get("default") for name in names & properties.keys()
                    if isinstance(properties[name], dict) and "default" in properties[name]]
        if len(defaults) == 1:
            path, path_kind = defaults[0], "path_hint"
        elif (not defaults and set(properties) <= names | selectors
              and arguments.keys() <= properties.keys()
              and not names.intersection(definition["parameters"].get("required", []))):
            # 只承认命令实际返回的资料记录，不从工具代码或 stdout 推断文件路径。
            path, path_kind = None, None
        else:
            return None
    if path_kind is not None and (not isinstance(path, str) or not path or len(path) > 512 or "\\" in path
            or any(ord(char) < 32 for char in path)
            or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive
            or any(part in {".", "..", ""} for part in path.split("/"))):
        return None
    for key, value in arguments.items():
        if key in path_keys:
            continue
        if key in {"limit", "offset", "page"}:
            if type(value) is not int or not 0 <= value <= 1000000:
                return None
        else:
            values = value if key in {"ids", "item_ids"} else [value]
            if (not isinstance(values, list) or not 1 <= len(values) <= 128
                    or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", item)
                           for item in values)):
                return None
    return path, path_kind


def _record_collection(value: object) -> bool:
    """无路径的工具回报须含可区分的资料记录，不接纳状态回执。"""
    if isinstance(value, dict):
        if value.get("ok", True) is not True:
            return False
        items = value.get("items", value.get("records"))
        if "count" in value and (type(value["count"]) is not int
                                or not isinstance(items, list) or value["count"] != len(items)):
            return False
    else:
        items = value
    if not isinstance(items, list) or not items:
        return False
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            return False
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in seen:
            return False
        if not any(isinstance(item.get(key), str) and item[key].strip()
                   for key in ("text", "body", "content", "description")):
            return False
        seen.add(item_id)
    return True


def _custom_stdout(content: str, *, require_records: bool = False) -> str | None:
    prefix = '[退出码] 0\n[stdout]\n'
    if not content.startswith(prefix):
        return None
    stdout = content[len(prefix):].strip()
    if "[输出已截断]" in stdout or "已截断" in stdout:
        return None
    try:
        value = json.loads(stdout, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None
    # 常见写入回执仅报告产物状态，不能作为读取材料。
    if isinstance(value, dict) and {"saved", "written", "created", "updated", "deleted", "output_path"} & value.keys():
        return None
    if require_records and not _record_collection(value):
        return None
    return stdout if isinstance(value, (dict, list)) else None


def _strict_json(text: str) -> object:
    from agent_core.core.flow_handoff import _depth, _exact_float, _unique
    value = json.loads(text, object_pairs_hook=_unique, parse_float=_exact_float,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    _depth(value)
    return value


def _same(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _receipt_echo(arguments: object, content: str) -> tuple[dict, list[str]] | None:
    prefix = '[退出码] 0\n[stdout]\n'
    if not isinstance(arguments, str) or len(arguments) > 16384 or len(content) > 16384 or not content.startswith(prefix):
        return None
    try:
        args = _strict_json(arguments)
        result = _strict_json(content[len(prefix):])
        if (not isinstance(args, dict) or not 0 < len(args) <= 32 or not isinstance(result, dict)
                or result.get('ok') is not True or not all(isinstance(k, str) and len(k) <= 128 for k in args)):
            return None
        matches = [v for v in result.values() if isinstance(v, dict)
                   and all(k in v and _same(v[k], x) for k, x in args.items())]
        if len(matches) != 1:
            return None
        return matches[0], sorted(args)
    except (ValueError, TypeError, RecursionError):
        return None


def _record_key(record: dict) -> str:
    return json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _echo_markers(text: str, echoes: dict[str, list[tuple[int, list[str]]]], before: int) -> list[dict]:
    # 只识别完整且唯一的 JSONL 行，不猜测未知包装。
    lines = text.splitlines()
    if not lines or len(lines) > 128 or any(not line.strip() for line in lines):
        return []
    try:
        records = [_strict_json(line) for line in lines]
        if any(not isinstance(row, dict) for row in records):
            return []
        keys = [_record_key(row) for row in records]
        counts = Counter(keys)
        markers = []
        offset = 0
        for line, key in zip(text.splitlines(keepends=True), keys, strict=True):
            body = line.rstrip('\r\n')
            matches = echoes.get(key, [])
            if len(matches) == 1 and matches[0][0] < before and counts[key] == 1:
                markers.append({'record_start': offset, 'record_end': offset + len(body),
                                'digest': hashlib.sha256(body.encode('utf-8', 'surrogatepass')).hexdigest(),
                                'fields': matches[0][1]})
            offset += len(line)
        return markers
    except (ValueError, TypeError, RecursionError):
        return []


def review_sources(record: dict, *, char_limit: int) -> list[dict]:
    """返回完整观察区间；预算按正文字符计，不表示整个文件完整。"""
    if type(char_limit) is not int or char_limit <= 0 or not isinstance(record, dict):
        return []
    messages, run = record.get("messages"), record.get("last_run")
    if not isinstance(messages, list) or not isinstance(run, dict):
        return []
    if not all(isinstance(message, dict) for message in messages):
        return []
    current = next((i for i in range(len(messages) - 1, -1, -1)
                    if is_user_request(messages[i])), -1)
    turn, events = run.get("turn_id"), run.get("tools")
    if (current < 0 or run.get("input_index") != current or not isinstance(turn, str)
            or not turn or not isinstance(events, list)):
        return []
    call_counts, result_counts, result_events = Counter(), Counter(), Counter()
    for message in messages[current + 1:]:
        calls = message.get("tool_calls", []) if message.get("role") == "assistant" else []
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, dict) and isinstance(call.get("id"), str):
                    call_counts[call["id"]] += 1
        if message.get("role") == "tool":
            identifier, event_id = message.get("tool_call_id"), message.get("_event_id")
            if isinstance(identifier, str):
                result_counts[identifier] += 1
            if isinstance(event_id, str):
                result_events[event_id] += 1
    event_counts = Counter(event.get("event_id") for event in events
                           if isinstance(event, dict) and isinstance(event.get("event_id"), str))
    event_call_counts = Counter(event.get("tool_call_id") for event in events
                                if isinstance(event, dict) and isinstance(event.get("tool_call_id"), str))
    event_by_id = {event["event_id"]: event for event in events
                   if isinstance(event, dict) and isinstance(event.get("event_id"), str)}
    rows = []
    echoes = []
    echo_overflow = False
    pending_echoes = []
    custom_rows = []
    custom_names = _custom_names(record)
    seen_spans = set()
    remaining = char_limit
    for index in range(current + 1, len(messages)):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(calls, list) or not calls:
            continue
        results = []
        position = index + 1
        while position < len(messages) and messages[position].get("role") == "tool":
            results.append(messages[position])
            position += 1
        for call in calls:
            if not isinstance(call, dict):
                continue
            identifier, function = call.get("id"), call.get("function")
            if (not isinstance(identifier, str) or not identifier or call_counts[identifier] != 1
                    or result_counts[identifier] != 1 or event_call_counts[identifier] != 1
                    or call.get("type") != "function" or not isinstance(function, dict)):
                continue
            name = function.get("name")
            if not isinstance(name, str) or (name not in {"read_file", "rag_search"} and name not in custom_names):
                continue
            matches = [result for result in results if result.get("tool_call_id") == identifier]
            if len(matches) != 1:
                continue
            result = matches[0]
            event_id = result.get("_event_id")
            if not isinstance(event_id, str) or not event_id or result_events[event_id] != 1 or event_counts[event_id] != 1:
                continue
            event = event_by_id[event_id]
            content = result.get("content")
            if (result.get("_provenance") != "executed" or event.get("source") != "executed"
                    or event.get("name") != name or event.get("tool_call_id") != identifier
                    or event.get("turn_id") != turn or event.get("outcome") != "completed"
                    or event.get("execution_progress") is not True
                    or not isinstance(content, str) or tool_outcome(content) != "completed"):
                continue
            try:
                arguments = json.loads(function.get("arguments", ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(arguments, dict) or (name == "rag_search" and arguments.get("source") == "history"):
                continue
            if name in custom_names:
                if event.get('action_stage') == 'commit':
                    echo = _receipt_echo(function.get('arguments'), content)
                    if echo is not None:
                        if len(echoes) < 128:
                            echoes.append(echo)
                        else:
                            echo_overflow = True
                path_info = _custom_args(arguments, custom_names[name])
                if path_info is not None:
                    stdout = _custom_stdout(content, require_records=path_info[1] is None)
                    if stdout is not None:
                        custom_rows.append((name, *path_info, stdout))
                continue
            observation = validate_observation(content, result.get("_context_observation"))
            if observation is None or observation.reader_tool != name:
                continue
            key = "path" if name == "read_file" else "item_ref"
            value = arguments.get(key)
            source = arguments.get("source", "") if name == "rag_search" else ""
            identified = (isinstance(value, str) and bool(value) and len(value) <= 1024
                          and isinstance(source, str))
            for start, end in observation.body_spans:
                text = content[start:end]
                identity = (name, source, value, text) if identified else None
                if len(text) > remaining or (identity is not None and identity in seen_spans):
                    continue
                row = {"id": f"S{len(rows) + 1}", "kind": name, "text": text,
                       "scope": "observed_span"}
                if isinstance(value, str) and value and len(value) <= 1024:
                    row[key] = value
                if name == 'read_file' and echoes:
                    pending_echoes.append((row, len(echoes)))
                rows.append(row)
                if identity is not None:
                    seen_spans.add(identity)
                remaining -= len(text)
    remaining = _append_user_input(record, messages[current], turn, rows, remaining)
    # 工具报告使用剩余预算，且最多占总预算的一半。
    budget = min(remaining, char_limit // 2)
    strong = {(row.get("path"), row["text"]) for row in rows if row.get("kind") == "read_file"}
    seen = set()
    for name, path, path_kind, stdout in custom_rows:
        identity = (name, path, path_kind, stdout)
        if (len(stdout) > budget or identity in seen or (path_kind == "path" and (path, stdout) in strong)):
            continue
        row = {"id": f"T{len(seen) + 1}", "kind": "custom_result", "scope": "tool_report",
               "tool": name, "text": stdout}
        if path_kind:
            row[path_kind] = path
        rows.append(row)
        seen.add(identity)
        budget -= len(stdout)
        remaining -= len(stdout)
    # 所有原始来源优先；可选标记只使用剩余预算。
    if not echo_overflow and pending_echoes:
        index = {}
        for i, (obj, fields) in enumerate(echoes):
            index.setdefault(_record_key(obj), []).append((i, fields))
        for row, before in pending_echoes:
            markers = _echo_markers(row['text'], index, before)
            cost = len(json.dumps(markers, ensure_ascii=True)) + 256 if markers else 0
            if markers and cost <= remaining:
                row['_submitted_echoes'] = markers
                remaining -= cost
    return rows


def _append_user_input(record: dict, message: dict, turn: str, rows: list[dict], remaining: int) -> int:
    marker, text, owner = message.get("_flow_user_input"), message.get("content"), record.get("id")
    if (not isinstance(marker, dict) or set(marker) != {"owner", "turn", "start", "end", "full_digest", "span_digest"}
            or not isinstance(owner, str) or not owner or marker.get("owner") != owner
            or marker.get("turn") != turn or not isinstance(text, str)):
        return remaining
    start, end = marker.get("start"), marker.get("end")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        return remaining
    handoff = message.get("_flow_handoff")
    if handoff is not None:
        if not isinstance(handoff, dict):
            return remaining
        handoff_start, handoff_end = handoff.get("start"), handoff.get("end")
        if (type(handoff_start) is not int or type(handoff_end) is not int
                or not 0 <= handoff_start < handoff_end <= len(text)
                or start < handoff_end and handoff_start < end):
            return remaining
    fragment = text[start:end]
    try:
        full_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        span_digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return remaining
    if (marker.get("full_digest") == full_digest and marker.get("span_digest") == span_digest
            and len(fragment) <= remaining):
        rows.append({"id": "U1", "kind": "user_input", "text": fragment, "scope": "user_report"})
        remaining -= len(fragment)
    return remaining
