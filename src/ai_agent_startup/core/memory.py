"""可读条目文件和有界相关记忆选择；兼容旧 Markdown。"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_agent_startup import config
from ai_agent_startup.rag.lexical import BM25Index


Tag = Annotated[str, Field(min_length=1, max_length=32)]


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str | None = Field(default=None, min_length=1)
    tags: list[Tag] | None = Field(default=None, max_length=8)
    source: str | None = Field(default=None, min_length=1, max_length=64)
    expires_at: datetime | None = None

    @field_validator('text', 'tags', 'source')
    @classmethod
    def no_explicit_null(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError('文本、标签与来源不能显式为空值')
        return value.strip() if isinstance(value, str) else value

    @field_validator('expires_at')
    @classmethod
    def expiry_timezone(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('过期时间必须带时区')
        return value


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = ''
    tags: list[Tag] = Field(default_factory=list, max_length=8)
    source: str | None = None
    include_expired: bool = False


def filter_entries(entries, *, tags=None, source=None, include_expired=False):
    selected = []
    for entry in entries:
        if tags and not set(tags).issubset(entry.get('tags', [])):
            continue
        if source is not None and entry.get('source') != source:
            continue
        if not include_expired and entry.get('expires_at'):
            try:
                expiry = datetime.fromisoformat(entry['expires_at'])
                if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                    continue
            except (TypeError, ValueError):
                # 损坏的到期元数据不作为有效事实注入；原始文件不删除。
                continue
        selected.append(entry)
    return selected


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
    if config.ENABLE_MEMORY_MANAGEMENT:
        entries = filter_entries(entries)
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
