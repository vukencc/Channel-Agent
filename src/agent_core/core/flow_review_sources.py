"""从本轮已执行的只读工具结果中收集可核验的原文片段。"""
from collections import Counter
import hashlib
import json

from agent_core.core.instrumentation import tool_outcome
from agent_core.core.messages import is_user_request
from agent_core.core.tool_observations import validate_observation


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
            if name not in {"read_file", "rag_search"}:
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
            observation = validate_observation(content, result.get("_context_observation"))
            if observation is None or observation.reader_tool != name:
                continue
            try:
                arguments = json.loads(function.get("arguments", ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(arguments, dict) or (name == "rag_search" and arguments.get("source") == "history"):
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
                rows.append(row)
                if identity is not None:
                    seen_spans.add(identity)
                remaining -= len(text)
    message = messages[current]
    marker, text, owner = message.get("_flow_user_input"), message.get("content"), record.get("id")
    if (not isinstance(marker, dict) or set(marker) != {"owner", "turn", "start", "end", "full_digest", "span_digest"}
            or not isinstance(owner, str) or not owner or marker.get("owner") != owner
            or marker.get("turn") != turn or not isinstance(text, str)):
        return rows
    start, end = marker.get("start"), marker.get("end")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        return rows
    handoff = message.get("_flow_handoff")
    if handoff is not None:
        if not isinstance(handoff, dict):
            return rows
        handoff_start, handoff_end = handoff.get("start"), handoff.get("end")
        if (type(handoff_start) is not int or type(handoff_end) is not int
                or not 0 <= handoff_start < handoff_end <= len(text)
                or start < handoff_end and handoff_start < end):
            return rows
    fragment = text[start:end]
    try:
        full_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        span_digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return rows
    if (marker.get("full_digest") == full_digest and marker.get("span_digest") == span_digest
            and len(fragment) <= remaining):
        rows.append({"id": "U1", "kind": "user_input", "text": fragment, "scope": "user_report"})
    return rows
