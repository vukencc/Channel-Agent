"""按不可变 RawHistory 的真实 JSON token 定位顶层配对工具回执。"""
from __future__ import annotations

import json
import re
from typing import NoReturn


TOOL_NAME_PATTERN = r'^[A-Za-z_][A-Za-z0-9_-]{0,63}$'
_WHITESPACE = ' \t\r\n'


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('历史 JSON 含重复键，不能确定回执位置')
        result[key] = value
    return result


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError('历史 JSON 含无效常量：' + value)


def _skip_whitespace(raw: str, index: int) -> int:
    while index < len(raw) and raw[index] in _WHITESPACE:
        index += 1
    return index


def _value_ranges(raw: str, start: int, end: int, message: dict,
                  decoder: json.JSONDecoder) -> dict[str, tuple[int, int]]:
    """遍历实际消息对象的键和值；不按重序列化长度或正文匹配猜测位置。"""
    cursor = _skip_whitespace(raw, start + 1)
    ranges = {}
    while cursor < end and raw[cursor] != '}':
        key, cursor = decoder.raw_decode(raw, cursor)
        if not isinstance(key, str) or key in ranges:
            raise ValueError('历史消息 JSON 键无效或重复')
        cursor = _skip_whitespace(raw, cursor)
        if cursor >= end or raw[cursor] != ':':
            raise ValueError('历史消息 JSON 缺少键值分隔')
        begin = _skip_whitespace(raw, cursor + 1)
        value, cursor = decoder.raw_decode(raw, begin)
        if key not in message or value != message[key]:
            raise ValueError('历史消息解析值与原始 JSON 不一致')
        ranges[key] = (begin, cursor)
        cursor = _skip_whitespace(raw, cursor)
        if cursor < end and raw[cursor] == ',':
            cursor = _skip_whitespace(raw, cursor + 1)
        else:
            break
    if cursor != end - 1 or raw[cursor] != '}' or ranges.keys() != message.keys():
        raise ValueError('历史消息 JSON 范围不完整')
    return ranges


def locate_tool_results(record: dict, tool_name: str) -> list[dict]:
    """返回真实配对回执的 JSON 值 token 区间；不证明执行或业务成功。

    offset/end_offset 使用 RawHistory 的 Python 字符索引，包含 content 字符串
    token 的引号；分页取得的是 JSON 编码原文片段，不是解码后的正文。
    """
    if type(tool_name) is not str or not re.fullmatch(TOOL_NAME_PATTERN, tool_name):
        raise ValueError('历史过滤工具名无效')
    raw = record.get('RawHistory')
    time = record.get('Time', {})
    if not isinstance(time, dict):
        raise ValueError('历史回执位置元数据无效')
    start_index, end_index = time.get('start'), time.get('end')
    if (not isinstance(raw, str) or type(start_index) is not int
            or type(end_index) is not int or start_index < 0 or end_index < start_index
            or not isinstance(record.get('ID'), str)
            or not re.fullmatch(r'[a-f0-9]{32}', record['ID'])):
        raise ValueError('历史回执定位元数据无效')
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    cursor = _skip_whitespace(raw, 0)
    if cursor >= len(raw) or raw[cursor] != '[':
        raise ValueError('历史原文必须是消息数组')
    cursor = _skip_whitespace(raw, cursor + 1)
    pending = {}
    results = []
    message_count = 0
    while cursor < len(raw) and raw[cursor] != ']':
        begin = cursor
        message, cursor = decoder.raw_decode(raw, cursor)
        if not isinstance(message, dict):
            raise ValueError('历史消息必须是 JSON 对象')
        ranges = _value_ranges(raw, begin, cursor, message, decoder)
        role = message.get('role')
        if role not in {'system', 'user', 'assistant', 'tool'}:
            raise ValueError('历史消息角色无效')
        if pending and role != 'tool':
            raise ValueError('历史工具请求与紧随回执未完整配对')
        if role == 'assistant':
            calls = message.get('tool_calls', [])
            if not isinstance(calls, list):
                raise ValueError('历史工具批次无效')
            for call in calls:
                function = call.get('function') if isinstance(call, dict) else None
                identifier = call.get('id') if isinstance(call, dict) else None
                name = function.get('name') if isinstance(function, dict) else None
                if (not isinstance(identifier, str) or not identifier or identifier in pending
                        or not isinstance(name, str) or not re.fullmatch(TOOL_NAME_PATTERN, name)
                        or call.get('type', 'function') != 'function'):
                    raise ValueError('历史工具批次标识或名称无效')
                pending[identifier] = name
        elif role == 'tool':
            identifier = message.get('tool_call_id')
            if not isinstance(identifier, str) or identifier not in pending:
                raise ValueError('历史含未配对或错位的工具回执')
            name = pending.pop(identifier)
            if not isinstance(message.get('content'), str):
                raise ValueError('历史工具回执正文必须是字符串')
            if name == tool_name:
                start, end = ranges['content']
                results.append({'tool_name': name, 'call_id': identifier,
                    'message_index': start_index + message_count,
                    'read_args': {'chunk_id': record['ID'], 'before': 0, 'after': 0,
                                  'offset': start, 'limit': min(6000, end - start)},
                    'end_offset': end})
        message_count += 1
        cursor = _skip_whitespace(raw, cursor)
        if cursor < len(raw) and raw[cursor] == ',':
            cursor = _skip_whitespace(raw, cursor + 1)
            if cursor >= len(raw) or raw[cursor] == ']':
                raise ValueError('历史消息数组含无效尾逗号')
        else:
            break
    if (cursor >= len(raw) or raw[cursor] != ']'
            or _skip_whitespace(raw, cursor + 1) != len(raw)
            or pending or start_index + message_count != end_index):
        raise ValueError('历史消息数组范围或工具配对不完整')
    return results
