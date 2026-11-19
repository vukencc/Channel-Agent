"""Flow 读取停滞判断；工具回执不代表业务验收。"""
import json
import hashlib
import re
from collections import Counter

from agent_core.core.execution_guidance import WORK_TOOLS
from agent_core.core.instrumentation import tool_outcome
from agent_core.core.messages import is_user_request
from agent_core.core.context import history_size, request_tokens
from agent_core.core.flow_handoff import _exact_float, validate_handoff

REVIEW_INSTRUCTION = ('\n[本次请求专用的 Flow 进度判断指令] 本次请求临时覆盖此前要求直接输出业务对象的输出格式；'
    '原交接契约仍约束 handoff 的 output 内容，原业务对象放在 output 中，后续仍按原契约校验。'
    '根据已有工具回执和本岗位交付条件判断；历史成功不证明当前验收通过。'
    '禁止工具调用。只输出 JSON 对象，decision 为 handoff、continue 或 blocked；'
    'output 为非空字符串，或在 handoff 时为业务对象。'
    'handoff 的 output 是本岗位最终正文；结构化交接时可直接给原业务 JSON 对象。'
    'continue 的 output 是具体缺失证据或下一步；blocked 的 output 是阻塞原因和安全续跑步骤。')


def review_candidate(record: dict) -> str | None:
    if not record.get('flow_id'):
        return None
    messages = record.get('messages', [])
    current = next((i for i in range(len(messages) - 1, -1, -1) if is_user_request(messages[i])), -1)
    if current < 0:
        return None
    event_rows = [e for e in record.get('last_run', {}).get('tools', [])
                  if isinstance(e, dict) and isinstance(e.get('event_id'), str) and e.get('event_id')]
    event_counts = Counter(e['event_id'] for e in event_rows)
    events = {e['event_id']: e for e in event_rows if event_counts[e['event_id']] == 1}
    work = None
    attempted_work = False
    reads = []
    for i, message in enumerate(messages):
        calls = message.get('tool_calls', []) if message.get('role') == 'assistant' else []
        if not isinstance(calls, list) or not calls:
            continue
        if i > current and any(isinstance(call, dict) and isinstance(call.get('function'), dict)
                               and call['function'].get('name') in WORK_TOOLS for call in calls):
            attempted_work = True
        pending = {c.get('id'): c for c in calls if isinstance(c, dict) and isinstance(c.get('id'), str) and c.get('id')}
        if len(pending) != len(calls):
            if i > current:
                reads = []
            continue
        j = i + 1
        batch = []
        event_ids = set()
        while j < len(messages) and messages[j].get('role') == 'tool':
            result = messages[j]
            j += 1
            ident = result.get('tool_call_id')
            call = pending.pop(ident, None)
            if not call or result.get('_provenance') != 'executed' or not isinstance(result.get('_event_id'), str) or not result.get('_event_id') or result['_event_id'] in event_ids:
                batch = []
                break
            event_ids.add(result['_event_id'])
            if event_counts[result['_event_id']] > 1:
                batch = []
                break
            fn = call.get('function', {})
            if not isinstance(fn, dict):
                batch = []
                break
            event = events.get(result['_event_id'])
            if event is not None and (event.get('source') != 'executed' or event.get('name') != fn.get('name') or event.get('tool_call_id') != ident):
                batch = []
                break
            if event is None and i > current:
                batch = []
                break
            try:
                args = json.loads(fn.get('arguments', ''))
            except (ValueError, TypeError):
                batch = []
                break
            if not isinstance(args, dict):
                batch = []
                break
            outcome = event.get('outcome') if event is not None else tool_outcome(result.get('content', ''))
            batch.append((fn.get('name'), args, result, outcome))
        if pending or len(batch) != len(calls):
            if i > current:
                reads = []
            continue
        for name, args, result, outcome in batch:
            if name in WORK_TOOLS and outcome == 'completed':
                work = result['_event_id']
                reads = []
            elif i > current and name in {'read_file', 'list_files'} and outcome == 'completed':
                reads.append((name, json.dumps(args, sort_keys=True, ensure_ascii=True), str(result.get('content', ''))))
            elif i > current:
                reads = []
    if work and (len(reads) >= 6 or any(count >= 3 for count in Counter(reads).values())):
        return work
    if not work and not attempted_work:
        file_reads = [item for item in reads if item[0] == 'read_file'
                      and item[2].strip() and not item[2].startswith('[未找到]')
                      and not re.match(r'^\[文件分页\].*\bchars=0(?:\s|$)', item[2])]
        counts = Counter(file_reads)
        for item in reversed(file_reads):
            if counts[item] >= 3:
                raw = json.dumps(item, ensure_ascii=True, separators=(',', ':')).encode('ascii')
                return 'read:' + hashlib.sha256(raw).hexdigest()
    return None


def review_history(history: list[dict], metrics: dict, char_limit: int, token_limit: int) -> list[dict] | None:
    """只在普通窗口及其保留空间不受挤压时追加判断。"""
    candidate = [*history, {'role': 'system', 'content': REVIEW_INSTRUCTION, '_context_reference': True}]
    if (history_size(candidate) + metrics.get('reserved_chars', 0) > char_limit
            or request_tokens(candidate) + metrics.get('reserved_tokens', 0) > token_limit):
        return None
    return candidate


def parse_review(content: str, contract: dict | None = None) -> tuple[str, str] | None:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    if not isinstance(content, str) or len(content) > 65536:
        return None
    try:
        content.encode('utf-8')
        value = json.loads(content, object_pairs_hook=unique, parse_float=_exact_float,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid constant')))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    if set(value) != {'decision', 'output'}:
        if contract is not None and not ({'decision', 'output'} & value.keys()):
            if validate_handoff(content, contract)[1] is None:
                return 'handoff', content
        return None
    decision, output = value['decision'], value['output']
    try:
        json.dumps(output, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None
    if decision == 'handoff' and isinstance(output, dict):
        output = json.dumps(output, ensure_ascii=False, allow_nan=False)
    if not isinstance(decision, str) or decision not in {'handoff', 'continue', 'blocked'} or not isinstance(output, str) or not output.strip():
        return None
    return decision, output
