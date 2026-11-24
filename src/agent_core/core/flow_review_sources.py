"""收集当前 Flow 可核验的直接输入和工具结果原文。"""
from collections import Counter
from collections.abc import Iterator
import hashlib
import json
from pathlib import PurePath, PurePosixPath, PureWindowsPath
import re

from agent_core.core.custom_tool_results import custom_stdout_body, parse_custom_stdout_json
from agent_core.core.instrumentation import tool_outcome
from agent_core.core.messages import is_user_request
from agent_core.core.tool_observations import validate_observation
from agent_core.core.workspace_memory import DIR_NAME, FILE_NAME



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
    stdout = custom_stdout_body(content)
    if stdout is None or "\n[stderr]\n" in content:
        return None
    stdout = stdout.strip()
    try:
        value = parse_custom_stdout_json(stdout)
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


def _workspace_memory_entry(path: object) -> bool:
    """只识别本平台相对工作区的记忆入口，不推断文件实际作者或链接目标。"""
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    value = PurePath(path)
    return (not value.is_absolute() and not value.drive and not value.root
            and value == PurePath(DIR_NAME, FILE_NAME))


def _message_tool_calls(message: dict) -> list[dict]:
    calls = message.get('tool_calls') if message.get('role') == 'assistant' else None
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _paired_tool_messages(messages: list[dict]) -> Iterator[tuple[dict, dict]]:
    """工具调用、随后的结果与事件标识必须在这段记录中各自唯一。"""
    calls = Counter(call['id'] for message in messages for call in _message_tool_calls(message)
                    if isinstance(call.get('id'), str))
    results = Counter(message['tool_call_id'] for message in messages
                      if message.get('role') == 'tool' and isinstance(message.get('tool_call_id'), str))
    result_events = Counter(message['_event_id'] for message in messages
                            if message.get('role') == 'tool' and isinstance(message.get('_event_id'), str))
    for index, message in enumerate(messages):
        batch = _message_tool_calls(message)
        if not batch:
            continue
        matching = {}
        position = index + 1
        while position < len(messages) and messages[position].get('role') == 'tool':
            result = messages[position]
            identifier = result.get('tool_call_id')
            if isinstance(identifier, str) and results[identifier] == 1:
                matching[identifier] = result
            position += 1
        for call in batch:
            identifier, function = call.get('id'), call.get('function')
            if (not isinstance(identifier, str) or not identifier or calls[identifier] != 1
                    or call.get('type') != 'function' or not isinstance(function, dict)
                    or not isinstance(function.get('name'), str)):
                continue
            result = matching.get(identifier)
            if result is None:
                continue
            event_id = result.get('_event_id')
            if (isinstance(event_id, str) and event_id and result_events[event_id] == 1
                    and result.get('_provenance') == 'executed' and isinstance(result.get('content'), str)):
                yield call, result


def _live_source_pairs(messages: list[dict], events: list[dict], turn: str) -> Iterator[tuple[dict, dict, dict]]:
    event_counts = Counter(event["event_id"] for event in events
                           if isinstance(event, dict) and isinstance(event.get("event_id"), str))
    event_call_counts = Counter(event["tool_call_id"] for event in events
                                if isinstance(event, dict) and isinstance(event.get("tool_call_id"), str))
    event_by_id = {event["event_id"]: event for event in events
                   if isinstance(event, dict) and isinstance(event.get("event_id"), str)}
    for call, result in _paired_tool_messages(messages):
        identifier, event_id = call['id'], result['_event_id']
        if event_call_counts[identifier] != 1 or event_counts[event_id] != 1:
            continue
        event = event_by_id[event_id]
        if (event.get("source") == "executed" and event.get("name") == call['function']['name']
                and event.get("tool_call_id") == identifier and event.get("turn_id") == turn
                and event.get("outcome") == "completed" and event.get("execution_progress") is True):
            yield call['function'], result, event


def _prior_echoes(messages: list[dict], stop: int, custom_names: dict[str, dict]) -> dict | None:
    """旧工具结果只可限制回读的证明力，不直接成为续跑来源。"""
    start = next((idx for idx in range(stop - 1, -1, -1) if is_user_request(messages[idx])), -1)
    if start < 0:
        return None
    known = {}
    count = 0
    for call, result in _paired_tool_messages(messages[start + 1:stop]):
        function = call['function']
        if function['name'] not in custom_names:
            continue
        echo = _receipt_echo(function.get('arguments'), result['content'])
        if echo is not None:
            count += 1
            if count > 128:
                return None
            obj, fields = echo
            known.setdefault(_record_key(obj), []).append((0, fields))
    return known


def _resumed_source_pairs(messages: list[dict], current: int, restored: dict[str, dict],
                          custom_names: dict[str, dict]) -> Iterator[tuple[dict, dict, dict]]:
    from agent_core.core.flow_required_reads import required_read_pair

    live_ids = {call['id'] for message in messages[current + 1:] for call in _message_tool_calls(message)
                if isinstance(call.get('id'), str)}
    live_event_ids = {message['_event_id'] for message in messages[current + 1:]
                      if message.get('role') == 'tool' and isinstance(message.get('_event_id'), str)}
    restored_events = Counter(reference['event_id'] for reference in restored.values()
                              if isinstance(reference, dict) and isinstance(reference.get('event_id'), str))
    for identifier, reference in restored.items():
        if not isinstance(reference, dict) or 'arguments_sha256' not in reference:
            continue
        event_id = reference.get('event_id')
        if (identifier in live_ids or not isinstance(event_id, str)
                or event_id in live_event_ids or restored_events[event_id] != 1):
            continue
        pair = required_read_pair(messages, identifier, reference, current, set(custom_names))
        if pair is not None:
            function, result = pair
            restrictions = (_prior_echoes(messages, reference['call_index'], custom_names)
                            if function['name'] == 'read_file' else {})
            if restrictions is not None:
                yield function, result, restrictions


def review_sources(record: dict, *, char_limit: int,
                   restored_reads: dict[str, dict] | None = None,
                   restored_input: tuple[str, str] | None = None) -> list[dict]:
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
    rows = []
    echoes = []
    echo_overflow = False
    pending_echoes = []
    custom_rows = []
    custom_names = _custom_names(record)
    seen_spans = set()
    remaining = char_limit

    def source_pairs():
        for function, result, event in _live_source_pairs(messages[current + 1:], events, turn):
            yield function, result, event, None
        if restored_reads:
            for function, result, restrictions in _resumed_source_pairs(messages, current, restored_reads, custom_names):
                yield function, result, None, restrictions

    for function, result, event, restrictions in source_pairs():
        name = function["name"]
        if name not in {"read_file", "rag_search"} and name not in custom_names:
            continue
        content = result["content"]
        # 已配对的 read_file 成功回执由下方观察摘要和区间核验，正文可含任意协议字样。
        if name != "read_file" and tool_outcome(content, custom_tool=name in custom_names) != "completed":
            continue
        try:
            arguments = json.loads(function.get("arguments", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(arguments, dict) or (name == "rag_search" and arguments.get("source") == "history"):
            continue
        if name in custom_names:
            if event is not None and event.get('action_stage') == 'commit':
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
            if name == "read_file" and _workspace_memory_entry(value):
                row["workspace_memory_path"] = True
            if name == 'read_file':
                if restrictions:
                    markers = _echo_markers(text, restrictions, 1)
                    if markers:
                        cost = len(json.dumps(markers, ensure_ascii=True)) + 256
                        if len(text) + cost > remaining:
                            # 已知回显不能在省去溯源标记后成为独立原文。
                            continue
                        row['_submitted_echoes'] = markers
                        remaining -= cost
                if event is not None and echoes:
                    pending_echoes.append((row, len(echoes)))
            rows.append(row)
            if identity is not None:
                seen_spans.add(identity)
            remaining -= len(text)
    fragment = _verified_user_input(record, messages[current], turn)
    if restored_input is not None and '_flow_user_input' not in messages[current]:
        fragment = _verified_restored_input(record, messages, current, restored_input)
    if fragment is not None and len(fragment) <= remaining:
        rows.append({"id": "U1", "kind": "user_input", "text": fragment, "scope": "user_report"})
        remaining -= len(fragment)
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


def _verified_user_input(record: dict, message: dict, turn: str) -> str | None:
    marker, text, owner = message.get("_flow_user_input"), message.get("content"), record.get("id")
    if (not isinstance(marker, dict) or set(marker) != {"owner", "turn", "start", "end", "full_digest", "span_digest"}
            or not isinstance(owner, str) or not owner or marker.get("owner") != owner
            or marker.get("turn") != turn or not isinstance(text, str)):
        return None
    start, end = marker.get("start"), marker.get("end")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        return None
    handoff = message.get("_flow_handoff")
    if handoff is not None:
        if not isinstance(handoff, dict):
            return None
        handoff_start, handoff_end = handoff.get("start"), handoff.get("end")
        if (type(handoff_start) is not int or type(handoff_end) is not int
                or not 0 <= handoff_start < handoff_end <= len(text)
                or start < handoff_end and handoff_start < end):
            return None
    fragment = text[start:end]
    try:
        full_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        span_digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None
    if marker.get("full_digest") == full_digest and marker.get("span_digest") == span_digest:
        return fragment
    return None


def _verified_restored_input(record: dict, messages: list[dict], current: int,
                             restored_input: tuple[str, str]) -> str | None:
    turn, original_text = restored_input
    # 如果原轮的标记不唯一，不从历史中挑选一个作为来源。
    candidates = [message for index, message in enumerate(messages) if index < current
                  and isinstance(marker := message.get('_flow_user_input'), dict) and marker.get('turn') == turn]
    if len(candidates) == 1 and is_user_request(candidates[0]):
        fragment = _verified_user_input(record, candidates[0], turn)
        if fragment == original_text:
            return fragment
    return None
