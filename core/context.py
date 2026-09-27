"""Bound model requests without changing the durable transcript or tool-call pairing."""
import copy
import json

import config


def history_size(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False))


class ContextBudgetError(ValueError):
    """工作窗口不足，调用方应保存检查点而非丢弃记录。"""

    def __init__(self, metrics):
        self.metrics = metrics
        super().__init__(f'上下文预算不足：{metrics}。完整记录未删除；请缩小请求、减少记忆或新建会话。')


def build_model_history(messages: list[dict], *, schemas=None, memory_chars=0, extra_chars=0) -> tuple[list[dict], dict]:
    """Compact bulky historical file payloads; evict whole old turns only if necessary."""
    history = copy.deepcopy(messages)
    before = history_size(history)
    schema_chars = history_size(schemas) if schemas else 0
    limit = config.MODEL_INPUT_CHARS - schema_chars
    def metrics():
        return {'original_chars': before, 'sent_chars': history_size(history),
                'schema': schema_chars, 'memory': memory_chars, 'extra': extra_chars,
                'messages': history_size(history) - memory_chars - extra_chars,
                'total_chars': history_size(history) + schema_chars,
                'compacted_file_parts': compacted, 'omitted_turns': omitted}
    current = max((i for i, m in enumerate(history) if m['role'] == 'user'), default=1)
    recent = max(current, len(history) - 4)
    names = {}
    compacted = 0
    for i, message in enumerate(history):
        for call in message.get('tool_calls', []):
            function = call['function']
            names[call['id']] = function['name']
            if i >= recent or function['name'] not in {'create_file', 'update_file', 'edit_file', 'run_command'}:
                continue
            try:
                arguments = json.loads(function['arguments'])
                for key in ('content', 'old_text', 'new_text', 'command'):
                    value = arguments.get(key)
                    if isinstance(value, str) and len(value) > min(2000, config.FILE_READ_CHARS):
                        arguments[key] = f'[历史片段已省略：{len(value)} 字符；需要当前文件请分页 read_file，勿按本占位符写入]'
                        compacted += 1
                function['arguments'] = json.dumps(arguments, ensure_ascii=False)
            except (ValueError, AttributeError):
                continue
        if (i < current and message['role'] == 'tool'
                and names.get(message.get('tool_call_id')) == 'read_file'
                and len(message.get('content', '')) > config.FILE_READ_CHARS):
            message['content'] = '[历史文件读取内容已省略；当前文件可能已修改，请按需重新 read_file。]'
            compacted += 1
    omitted = 0
    budget = limit - (300 if compacted or before > limit else 0)
    # Long scans within one turn must also fit; keep recent pages and their tool pairs.
    for i, message in enumerate(history):
        if history_size(history) <= budget:
            break
        if (i < recent and message['role'] == 'tool'
                and names.get(message.get('tool_call_id')) == 'read_file'
                and len(message.get('content', '')) > 2000):
            message['content'] = '[较早的文件页已移出工作上下文；需要该片段时用 read_file 的 offset/search 重新读取。完整原文仍保存在会话记录。]'
            compacted += 1
    while history_size(history) > budget:
        turns = [i for i, m in enumerate(history) if m['role'] == 'user']
        if len(turns) < 2:
            raise ContextBudgetError(metrics())
        del history[turns[0]:turns[1]]
        omitted += 1
    if compacted or omitted:
        history[0]['content'] += (
            f'\n[上下文窗口说明] 历史文件片段省略 {compacted} 处，旧轮次省略 {omitted} 轮；'
            '完整原始对话仍保存在会话文件。省略内容不是空文件或已删除的事实，不得按占位符覆盖文件。'
        )
    if history_size(history) > limit:
        raise ContextBudgetError(metrics())
    return history, metrics()
