"""可读条目文件和有界相关记忆选择；兼容旧 Markdown。"""
import hashlib
import json

import config
from rag.lexical import BM25Index


def parse_entries(text: str) -> list[dict]:
    entries = []
    for line in text.splitlines():
        value = line.removeprefix('- ').strip()
        if not value:
            continue
        try:
            entry = json.loads(value)
            if not isinstance(entry, dict) or not isinstance(entry.get('text'), str) or not entry.get('id'):
                raise ValueError
        except (ValueError, TypeError):
            entry = {'id': hashlib.sha256(value.encode()).hexdigest()[:12], 'text': value,
                     'source': 'legacy', 'created_at': '', 'tags': []}
        entries.append(entry)
    return entries


def render_entries(entries: list[dict]) -> str:
    return ''.join('- ' + json.dumps(entry, ensure_ascii=False) + '\n' for entry in entries)


def select_memory(entries: list[dict], query: str) -> str:
    if not entries:
        return ''
    ranking = BM25Index([entry['text'] for entry in entries]).search(query, config.MEMORY_TOP_K) if query else []
    selected = [entries[index] for index, _ in ranking]
    if not selected:
        # 无命中时仅提供短摘录，不能把完整记忆无条件注入。
        selected = entries[-config.MEMORY_TOP_K:]
        prefix = '[记忆摘录：未找到关键词匹配]\n'
    else:
        prefix = '[相关记忆]\n'
    text = prefix + '\n'.join(f"- [{entry['id']}] {entry['text']}" for entry in selected)
    limit = min(config.MEMORY_INJECT_CHARS, config.MEMORY_MAX_CHARS)
    marker = '[记忆已截断]'
    return text if len(text) <= limit else text[:max(0, limit - len(marker))] + marker[:limit]
