"""时间线详情按需从所属会话读取；正文不进入追踪缓存，脱敏后再分页。"""
import json
import re

from agent_test.web.context_archive import ContextArchiveReader


_SECRET_KEY = re.compile(r'(?:api[_-]?key|password|passwd|access[_-]?token|refresh[_-]?token|authorization|secret|^token$)', re.I)
_ASSIGNMENT = re.compile(r'''(?ix)(\b(?:[a-z0-9_]*(?:api_?key|access_token|refresh_token|password|passwd|secret)|TOKEN)\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''')
_BEARER = re.compile(r'(?i)(\bauthorization\s*:\s*(?:bearer|basic)\s+)[^\s"\']+')
_JSON_FIELD = re.compile(r'''(["'])([A-Za-z0-9_.-]+)\1\s*:\s*''')
_MAX_DETAIL_CHARS = 1048576


def read_debug_detail(manager, session, event, offset: int, limit: int) -> dict:
    """调用方先校验 generation、事件窗口、会话归属并按需加载消息。"""
    reader = ContextArchiveReader(manager)

    def safe_text(text):
        text = reader._redact(text)
        # 工具结果常有协议前缀，不一定是完整 JSON；按字段解码值，先脱敏再转义/分页。
        parts, cursor = [], 0
        decoder = json.JSONDecoder()
        for match in _JSON_FIELD.finditer(text):
            if match.start() < cursor or not _SECRET_KEY.search(match[2]):
                continue
            try:
                _, end = decoder.raw_decode(text, match.end())
            except (ValueError, RecursionError):
                end = text.find('\n', match.end())
                if end < 0:
                    end = len(text)
            parts.extend((text[cursor:match.end()], '"[已隐藏敏感字段]"'))
            cursor = end
        parts.append(text[cursor:])
        text = ''.join(parts)
        text = _BEARER.sub(r'\1[已隐藏凭据]', text)
        return _ASSIGNMENT.sub(r'\1[已隐藏凭据]', text)

    def clean_fields(value):
        if isinstance(value, dict):
            return {key: '[已隐藏敏感字段]' if _SECRET_KEY.search(key) else clean_fields(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [clean_fields(item) for item in value]
        return safe_text(value) if isinstance(value, str) else value

    index = event.get('message_index')
    content, format_name = json.dumps(event, ensure_ascii=False, indent=2), 'metadata'
    if event['kind'] in {'model.final', 'tool.started', 'tool.completed'}:
        messages = session.record['messages']
        if type(index) is not int or not 0 <= index < len(messages):
            raise FileNotFoundError('事件原始消息已不可用')
        message = messages[index]
        if message.get('role') != 'assistant':
            raise FileNotFoundError('事件消息关联无效')
        if event['kind'] == 'model.final':
            if message.get('tool_calls'):
                raise FileNotFoundError('事件不是最终回复')
            content, format_name = message.get('content') or '', 'text'
        else:
            call = next((item for item in message.get('tool_calls', [])
                         if item['id'] == event.get('tool_call_id') and
                         item['function']['name'] == event.get('tool_name')), None)
            if call is None:
                raise FileNotFoundError('工具调用关联无效')
            arguments = call['function']['arguments']
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                pass
            result = None
            for following in messages[index + 1:]:
                if following.get('role') == 'assistant':
                    break
                if following.get('role') == 'tool' and following.get('tool_call_id') == call['id']:
                    result = following.get('content', '')
                    break

            content = json.dumps(clean_fields({'tool': event['tool_name'], 'arguments': arguments,
                'result': result, 'result_pending': result is None}), ensure_ascii=False, indent=2)
            format_name = 'json'
    content = safe_text(content)
    clipped = len(content) > _MAX_DETAIL_CHARS
    content = content[:_MAX_DETAIL_CHARS]
    if offset > len(content):
        raise ValueError('详情分页位置超出范围')
    end = min(offset + limit, len(content))
    return {'event': event, 'content': content[offset:end], 'format': format_name,
            'next_offset': end if end < len(content) else None,
            'total_chars': len(content), 'clipped': clipped}
