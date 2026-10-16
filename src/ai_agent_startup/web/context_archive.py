"""DEBUG 专用私有归档浏览：按需读取、完整性校验、分页及密钥脱敏。"""
import json
import os
import re
import stat

from ai_agent_startup.core.headless import redact_result
from ai_agent_startup.core.history_context import HistoryContext
from ai_agent_startup.core.maintenance import directory_handle


class ContextArchiveReader(HistoryContext):
    def __init__(self, manager):
        # 仅复用索引/原文校验；不启动监督器，不创建上下文运行状态。
        self.manager = manager

    @staticmethod
    def _read_json(path):
        try:
            with directory_handle(path.parent) as directory:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024 * 1024:
                        raise PermissionError('归档必须是有界且没有硬链接的普通文件')
                    content = stream.read(64 * 1024 * 1024 + 1)
                    if len(content) > 64 * 1024 * 1024:
                        raise PermissionError('归档读取超过容量上限')
                    return json.loads(content)
        except FileNotFoundError:
            raise
        except OSError:
            raise PermissionError('归档路径不能安全读取') from None

    def _redact(self, value):
        # 运行时模型与独立监督器不共享主模型配置，需补齐各自的已知密钥。
        secrets = []
        context = getattr(self.manager, 'history_context', None)
        supervisor = getattr(context, 'supervisor', None)
        key = getattr(getattr(supervisor, 'settings', None), 'api_key', '')
        if key:
            secrets.append(key)
        registry = getattr(self.manager, 'model_registry', None)
        if registry is not None:
            for identifier in registry.ids():
                try:
                    snapshot = registry.resolve(identifier)
                except ValueError:
                    continue
                if snapshot is not None:
                    secrets.append(snapshot.api_key)
        secrets.sort(key=len, reverse=True)

        def clean(item):
            if isinstance(item, str):
                for secret in secrets:
                    if secret:
                        item = item.replace(secret, '[已隐藏配置密钥]')
                return item
            if isinstance(item, dict):
                return {key: clean(child) for key, child in item.items()}
            if isinstance(item, list):
                return [clean(child) for child in item]
            return item

        return clean(redact_result(value))

    def paths(self, session):
        folder = self._folder(session)
        return {'manifest': str(folder / 'manifest.json'), 'chunks': str(folder / 'chunks'),
                'journal': str(self.manager.store.directory(session.id) / 'messages.jsonl')}

    def listing(self, session, offset=0, limit=20):
        manifest = self._load(session)
        chunks = manifest['chunks']
        if offset > len(chunks):
            raise ValueError('归档分页位置超出范围')
        page = chunks[offset:offset + limit]
        return self._redact({'paths': self.paths(session), 'format': 'json',
                              'revision': manifest['revision'], 'session_summary': self._redact(manifest['session_summary'])[:16000],
                              'summary_kind': manifest['summary_kind'],
                              'chunks': [{'ID': item['ID'], 'Time': item['Time'], 'Summary': self._redact(item['Summary'])[:16000],
                                          'summary_kind': item.get('summary_kind', 'unknown'),
                                          'path': str(self._folder(session) / 'chunks' / (item['ID'] + '.json'))}
                                         for item in page], 'total_chunks': len(chunks),
                              'next_offset': offset + len(page) if offset + len(page) < len(chunks) else None})

    def chunk(self, session, identifier, offset=0, limit=6000):
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('历史块标识无效')
        manifest = self._load(session)
        selected = [item for item in manifest['chunks'] if item['ID'] == identifier]
        if not selected:
            raise FileNotFoundError('当前会话不存在此归档块')
        record = self._read_records(session, selected)[0]
        # 先脱敏后分页，避免密钥跨分页边界泄露；磁盘原文保持不变。
        raw = self._redact(record['RawHistory'])
        if offset > len(raw):
            raise ValueError('原文读取位置超出范围')
        end = min(offset + limit, len(raw))
        return self._redact({'ID': record['ID'], 'Time': record['Time'], 'Summary': self._redact(record['Summary'])[:16000],
                              'summary_kind': record['summary_kind'],
                              'path': str(self._folder(session) / 'chunks' / (identifier + '.json')),
                              'RawHistory': raw[offset:end], 'raw_chars': len(raw),
                              'next_offset': end if end < len(raw) else None})
