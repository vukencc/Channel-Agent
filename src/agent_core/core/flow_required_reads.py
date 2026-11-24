"""从节点岗位指令识别明确的读取步骤；不授予工具执行权限。"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

from agent_core.core.custom_tool_results import custom_result_outcome
from agent_core.core.flow_action_preflight import selection_only
from agent_core.core.execution_guidance import READ_TOOLS
from agent_core.core.messages import is_user_request
from agent_core.core.tool_observations import extract_observation, validate_observation

_BUILTIN_READS = READ_TOOLS | {"web_search"}
_CONDITION = re.compile(r"(?:如果|若|假如|倘若|当.{0,20}时|仅在.{0,20}时|必要时|需要时|可选|可以|可调用|可使用|建议|例如|示例|不要|别调用|禁止|无需|不必|不需要|二选一|\b(?:if|when|unless|optional|example|suggest|avoid|do not|don't)\b)", re.I)
_CALL = re.compile(r"^(?:(?:\d+[.、)]|[-*])\s*)?(?:(?:必须|请|首先|先|务必|please|must|first)\s*)*(?:调用|使用|执行|call|invoke|use)\s*[`\"'‘“]*([A-Za-z_][A-Za-z0-9_-]{0,63})[`\"'’”]*(?=$|[\s，。,.;:：、（(])", re.I)
_READ = re.compile(r"(?:读取|读出|查询|检索|获取|只读|\bread\b|\bfetch\b|\bquery\b|\bretrieve\b)", re.I)
_WRITE = re.compile(r"(?:写入|修改|更新|删除|创建|提交|发送|\bwrite\b|\bupdate\b|\bdelete\b|\bcreate\b|\bsend\b)", re.I)


def successful_custom_result(result: object) -> bool:
    """必做读取只认可完整的成功回执。"""
    return custom_result_outcome(result) == 'completed'


def successful_builtin_result(name: str, result: object) -> bool:
    """按各内置工具现有的回执协议核验，不把正文解释为状态。"""
    if not isinstance(result, str):
        return False
    if not result:
        return name in {'read_file', 'web_search'}
    observation = extract_observation(result)
    if name == 'read_file':
        return bool(observation and observation['reader_tool'] == name) or result.startswith('[未找到] ')
    if name == 'list_files':
        return result.startswith('[目录] ')
    if name in {'history_read', 'history_search'}:
        try:
            value = json.loads(result)
        except (ValueError, TypeError, RecursionError):
            return False
        return isinstance(value, (list, dict)) and not (isinstance(value, dict) and value.get('error'))
    if name == 'rag_search':
        if result.startswith('[检索状态] '):
            return True
        try:
            return isinstance(json.loads(result), list)
        except (ValueError, TypeError, RecursionError):
            return False
    if name == 'web_search':
        return result.startswith('- [')
    # 未注册的历史名称没有当前可靠回执协议。
    return False


def _first_statement(text: str) -> str:
    """仅在括号外的句界拆分，括号内的分页说明属于同一调用。"""
    depth = 0
    for index, char in enumerate(text):
        if char in '（(':
            depth += 1
        elif char in '）)':
            depth = max(0, depth - 1)
        elif not depth and (char in '；;。' or
                (char == '.' and index > 0 and text[index - 1].isascii()
                 and (text[index - 1].isalpha() or text[index - 1] == '_')
                 and index + 1 < len(text) and text[index + 1].isspace())):
            return text[:index]
    return text


def required_reads(task: str, definitions: list[dict] | None) -> set[str]:
    """只接受有明确调用和读取意图的无条件步骤。"""
    known = set(_BUILTIN_READS)
    for definition in definitions or []:
        if not isinstance(definition, dict):
            continue
        description = definition.get("description", "")
        parameters = definition.get("parameters", {})
        name = definition.get("name")
        # 宽松的 schema 仍可表达选择器意图；仅检查副本，不改变预审或执行 schema。
        intent_schema = dict(parameters) if isinstance(parameters, dict) else {}
        intent_schema.setdefault('additionalProperties', False)
        empty_selector = (intent_schema.get('type') == 'object' and intent_schema.get('properties') == {}
                          and intent_schema.get('required', []) == []
                          and intent_schema.get('additionalProperties') is False
                          and not set(intent_schema) - {'type', 'properties', 'required', 'additionalProperties', 'description', 'title'})
        if (isinstance(name, str) and isinstance(description, str) and isinstance(parameters, dict)
                and (empty_selector or selection_only(intent_schema))
                and _READ.search(description)):
            # 描述只用于辨识用途，实际执行仍按原动态工具权限和预审处理。
            clean = re.sub(r"(?:不(?:做|进行)?(?:任何)?|不会|无需|禁止|not|never|without)\s*(?:修改|写入|更新|删除|创建|提交|write|modify|update|delete|create)", "", description, flags=re.I)
            if not _WRITE.search(clean):
                known.add(name)
    result: set[str] = set()
    conditional_indent: int | None = None
    conditional_list = False
    fenced = False
    for line in task.splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced:
            continue
        indent = len(line) - len(line.lstrip())
        is_list = bool(re.match(r"(?:\d+[.、)]|[-*])\s*", stripped))
        if conditional_indent is not None and (indent < conditional_indent or
                (indent == conditional_indent and not (conditional_list and is_list))):
            conditional_indent = None
            conditional_list = False
        if conditional_indent is not None:
            continue
        # 分号可分隔独立步骤；只对行首的调用语句核验。
        statement = _first_statement(stripped)
        parentheses = re.findall(r"（([^（）]*)）|\(([^()]*)\)", statement)
        # 只有明确限定选择参数的括号条件不改变调用本身的必需性。
        if any(_CONDITION.search(part) and not (re.search(r"\b(?:limit|offset|page|ids?|path|cursor)\b", part, re.I)
                and not re.search(r"调用|执行|call|invoke|客户|用户|要求", part, re.I))
               for pair in parentheses for part in pair if part):
            continue
        main = re.sub(r"（[^（）]*）|\([^()]*\)", "", statement)
        main = re.sub(r"[,，]\s*(?:必要时|需要时)\s*(?:分页|分批)\s*$", "", main)
        if _CONDITION.search(main):
            if stripped.endswith(('：', ':', '。', '.')):
                conditional_indent = indent
                conditional_list = not is_list and stripped.endswith(('：', ':'))
            continue
        match = _CALL.match(main.strip())
        if match:
            name = match.group(1)
            remainder = main.strip()[match.end():]
            alternative = re.search(r"(?:或|或者|\bor\b).*?(?:调用|使用|执行|call|invoke|use)?\s*[`\"'‘“]*([A-Za-z_][A-Za-z0-9_-]{0,63})", remainder, re.I)
            if name in known and not (alternative and alternative.group(1) in known):
                result.add(name)
    return result


def _receipt_digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def required_read_reference(name: str, call_index: int, result_index: int, message: dict,
                            arguments: str) -> dict:
    """记录已验证回执在会话日志中的位置及请求、正文摘要。"""
    return {'name': name, 'call_index': call_index, 'result_index': result_index,
            'event_id': message['_event_id'], 'sha256': _receipt_digest(message['content']),
            'arguments_sha256': _receipt_digest(arguments)}


def required_read_pair(messages: list[dict], identifier: str, reference: dict, limit: int,
                       custom: set[str]) -> tuple[dict, dict] | None:
    """只还原单个引用的真实调用与回执，不打开整个旧会话。"""
    if not isinstance(reference, dict):
        return None
    name, event_id = reference.get('name'), reference.get('event_id')
    call_index, result_index = reference.get('call_index'), reference.get('result_index')
    if (not isinstance(name, str) or not isinstance(event_id, str) or not event_id
            or type(call_index) is not int or type(result_index) is not int
            or not 0 < call_index < result_index < limit <= len(messages)):
        return None
    request, result = messages[call_index], messages[result_index]
    calls = request.get('tool_calls')
    if request.get('role') != 'assistant' or not isinstance(calls, list):
        return None
    match = [call for call in calls if isinstance(call, dict) and call.get('id') == identifier]
    if len(match) != 1 or match[0].get('type') != 'function':
        return None
    function = match[0].get('function')
    if (not isinstance(function, dict) or function.get('name') != name
            or not isinstance(function.get('arguments'), str)
            or result.get('tool_call_id') != identifier or result.get('_event_id') != event_id
            or result.get('_provenance') != 'executed' or not isinstance(result.get('content'), str)
            or any(message.get('role') != 'tool' for message in messages[call_index + 1:result_index + 1])
            or (name in custom and not successful_custom_result(result['content']))):
        return None
    actual = required_read_reference(name, call_index, result_index, result, function['arguments'])
    if 'arguments_sha256' not in reference:
        actual.pop('arguments_sha256')
    if actual != reference:
        return None
    return function, result


def resume_required_reads(record: dict, checkpoint: dict, required: set[str],
                          custom: set[str], *, require_commit: bool) -> tuple[set[str], dict[str, dict]]:
    """仅使用原待续跑 Flow 的成功读取，重新核对请求、事件和原始回执。"""
    messages = record['messages']
    limit = record['last_run']['input_index']
    if 'required_reads' in checkpoint:
        saved = checkpoint['required_reads']
    else:
        saved = _legacy_read_checkpoint(record, checkpoint['turn_id'], required, custom,
                                        require_commit=require_commit)
    if not isinstance(saved, dict) or not isinstance(saved.get('seen'), list) or not isinstance(saved.get('calls'), dict):
        return set(), {}
    seen = {identifier for identifier in saved['seen'] if isinstance(identifier, str) and identifier}
    verified = {}
    for identifier, reference in saved['calls'].items():
        if (identifier not in seen or not isinstance(reference, dict)
                or not isinstance(reference.get('name'), str) or reference['name'] not in required):
            continue
        pair = required_read_pair(messages, identifier, reference, limit, custom)
        if pair is not None:
            function, result = pair
            verified[identifier] = required_read_reference(
                reference['name'], reference['call_index'], reference['result_index'], result, function['arguments'])
    return seen, verified


def _legacy_read_checkpoint(record: dict, turn_id: str, required: set[str],
                            custom: set[str], *, require_commit: bool) -> dict:
    """旧检查点从已绑定的终态轮次恢复，不能把完成名称摘要当作证据。"""
    messages = record['messages']
    runs = [row for row in record.get('run_history', ())
            if isinstance(row, dict) and row.get('turn_id') == turn_id]
    if len(runs) != 1:
        return {}
    run = runs[0]
    start, limit = run.get('input_index'), record['last_run']['input_index']
    if (run.get('status') != 'checkpoint' or run.get('stop_reason') not in {'model_connection', 'model_timeout'}
            or type(start) is not int or not 0 < start < limit
            or not is_user_request(messages[start]) or not isinstance(run.get('tools'), list)):
        return {}
    calls, replies, events = defaultdict(list), defaultdict(list), defaultdict(list)
    for index in range(start + 1, limit):
        message = messages[index]
        if is_user_request(message):
            return {}
        if message.get('role') == 'assistant' and isinstance(message.get('tool_calls'), list):
            for call in message['tool_calls']:
                if isinstance(call, dict) and isinstance(call.get('id'), str) and call['id']:
                    calls[call['id']].append(index)
        elif message.get('role') == 'tool' and isinstance(message.get('tool_call_id'), str):
            replies[message['tool_call_id']].append(index)
    for event in run['tools']:
        if isinstance(event, dict) and isinstance(event.get('tool_call_id'), str):
            events[event['tool_call_id']].append(event)
    verified = {}
    for identifier, positions in calls.items():
        if len(positions) != 1 or len(replies[identifier]) != 1 or len(events[identifier]) != 1:
            continue
        event = events[identifier][0]
        name, result_index = event.get('name'), replies[identifier][0]
        result = messages[result_index]
        content = result.get('content')
        if (not isinstance(name, str) or name not in required or event.get('turn_id') != turn_id
                or event.get('source') != 'executed' or event.get('outcome') != 'completed'
                or event.get('execution_progress') is not True or not isinstance(content, str)
                or not isinstance(result.get('_event_id'), str) or result['_event_id'] != event.get('event_id')
                or (require_commit and name in custom and event.get('action_stage') != 'commit')):
            continue
        observation = validate_observation(content, result.get('_context_observation')) if name == 'read_file' else None
        if (name not in custom and (observation is None or observation.reader_tool != name)
                and ('[输出已截断]' in content or '已截断）' in content)):
            continue
        verified[identifier] = {'name': name, 'call_index': positions[0], 'result_index': result_index,
                                'event_id': result['_event_id'], 'sha256': _receipt_digest(content)}
    return {'seen': list(calls), 'calls': verified}
