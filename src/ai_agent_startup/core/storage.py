"""Atomic, human-readable session files. One CLI owns a state directory at a time."""
import asyncio
import copy
import concurrent.futures
import shutil
import threading
import hashlib

from ai_agent_startup import config
from ai_agent_startup.core.file_lock import lock_file, unlock_file
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def wait_for_io_completion(future):
    """即使界面被连续取消，也必须等实际文件操作结束再解除维护保护。"""
    while True:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.cancelled():
                raise


def finish_pending_tools(messages: list[dict]):
    """Keep API tool-call protocol valid after cancellation/crash, without replay."""
    pending = {}
    for message in messages:
        for call in message.get('tool_calls', []):
            pending[call['id']] = call
        if message['role'] == 'tool':
            pending.pop(message['tool_call_id'], None)
    for identifier in pending:
        messages.append({'role': 'tool', 'tool_call_id': identifier,
                         'content': '[中断] 执行结果未知，请先检查文件状态，勿自动重放操作。',
                         '_provenance': 'recovered_unknown'})


class LazyRecord(dict):
    """元数据可直接访问；首次读取消息时才执行完整校验与中断恢复。"""

    def __init__(self, metadata, loader):
        super().__init__(metadata)
        self.loader = loader
        self._load_lock = threading.RLock()

    def materialize(self):
        with self._load_lock:
            if self.loader is not None:
                record = self.loader()
                dict.clear(self)
                dict.update(self, record)
                self.loader = None
        return self

    def __getitem__(self, key):
        if key == 'messages':
            self.materialize()
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key == 'messages':
            self.materialize()
        return super().get(key, default)

    def __setitem__(self, key, value):
        self.materialize()
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        self.materialize()
        super().update(*args, **kwargs)

    def pop(self, *args):
        self.materialize()
        return super().pop(*args)


class SessionStore:
    def __init__(self, root: Path, workspace_root: Path | None = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / '.lock').open('a')
        try:
            lock_file(self.lock, blocking=False)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('该状态目录已有 CLI 在运行；请使用不同的 --state-dir') from None
        except BaseException:
            self.lock.close()
            raise
        self.errors: list[str] = []
        self.workspace_root = Path(workspace_root or config.SANDBOX_DIR).resolve()
        self.writer = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-save")
        self._workspace_lock = threading.Lock()
        self._memory_lock = threading.RLock()

    def close(self):
        self.writer.shutdown(wait=True)
        if not self.lock.closed:
            unlock_file(self.lock)
            self.lock.close()

    def directory(self, identifier: str) -> Path:
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('无效会话 ID')
        path = self.root / identifier
        if path.is_symlink():
            raise ValueError('会话目录不能是符号链接')
        return path

    def workspace_path(self, identifier: str) -> Path:
        self.directory(identifier)  # Validate ID before constructing a filesystem path.
        return self.workspace_root / identifier

    def check_session_archive(self, identifier: str) -> tuple[Path, Path]:
        """归档前只做验证，批量删除可以先检查整组会话。"""
        directory = self.directory(identifier)
        destination_root = self.root / '.trash'
        if destination_root.is_symlink():
            raise ValueError('会话回收区不能是符号链接')
        if destination_root.exists() and not destination_root.is_dir():
            raise ValueError('会话回收区必须是目录')
        if not directory.is_dir() or not (directory / 'session.json').is_file():
            raise ValueError('会话记录不存在')
        if (directory / 'workspace').exists() or (directory / 'workspace').is_symlink():
            raise ValueError('会话含旧版工作区；请先迁移或整理文件后删除会话')
        if self.workspace_path(identifier).resolve().is_relative_to(directory.resolve()):
            raise ValueError('工作区位于会话数据目录内，不能删除会话')
        destination = destination_root / identifier
        if destination.exists() or destination.is_symlink():
            raise ValueError('回收区已存在同名会话，拒绝覆盖')
        return directory, destination

    def delete_session(self, identifier: str) -> Path:
        """原子移入回收区；工作区、导出和共享记忆保留。"""
        directory, destination = self.check_session_archive(identifier)
        destination.parent.mkdir(exist_ok=True, mode=0o700)
        from ai_agent_startup.core.audit_writer import append_audit
        append_audit(self.root / 'session-lifecycle.jsonl', json.dumps({
            'event': 'session_delete_requested', 'session_id': identifier,
            'workspace_preserved': True, 'created_at': now()}, ensure_ascii=False), sync=config.AUDIT_SYNC)
        directory.rename(destination)
        return destination

    def delete_sessions(self, identifiers: list[str]) -> dict[str, Path]:
        """先验证整组；遇到文件系统错误时回滚已完成的原子重命名。"""
        for identifier in identifiers:
            self.check_session_archive(identifier)
        moved = {}
        try:
            for identifier in identifiers:
                moved[identifier] = self.delete_session(identifier)
        except BaseException:
            for identifier, archive in reversed(list(moved.items())):
                archive.rename(self.directory(identifier))
            from ai_agent_startup.core.audit_writer import append_audit
            append_audit(self.root / 'session-lifecycle.jsonl', json.dumps({
                'event': 'session_delete_rolled_back', 'session_ids': list(moved),
                'created_at': now()}, ensure_ascii=False), sync=config.AUDIT_SYNC)
            raise
        return moved

    def workspace(self, identifier: str) -> Path:
        """Copy legacy files once; preserve originals and refuse ambiguous overwrites."""
        destination = self.workspace_path(identifier)
        legacy = self.directory(identifier) / 'workspace'
        marker = self.directory(identifier) / 'workspace-migrated'
        with self._workspace_lock:
            if destination.is_symlink():
                raise ValueError('工作区目录不能是符号链接')
            if legacy.exists() and not marker.exists():
                if destination.exists():
                    raise ValueError(f'旧工作区与新工作区同时存在，未覆盖文件。请检查 {legacy} 和 {destination}')
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = Path(tempfile.mkdtemp(prefix='.migration-', dir=destination.parent))
                try:
                    shutil.copytree(legacy, temporary, dirs_exist_ok=True, symlinks=True)
                    temporary.rename(destination)
                    self.atomic_write(marker, str(destination))
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            destination.mkdir(parents=True, exist_ok=True)
        return destination

    def save_async(self, record: dict):
        """只复制小型元数据；消息追加后不可修改，计数界定本次快照。"""
        if isinstance(record, LazyRecord):
            record.materialize()
        record['updated_at'] = now()
        snapshot = copy.deepcopy({key: value for key, value in record.items() if key != 'messages'})
        snapshot['messages'] = record['messages']
        snapshot['_count'] = len(record['messages'])
        snapshot['_system'] = copy.deepcopy(record['messages'][0])
        return asyncio.get_running_loop().run_in_executor(self.writer, self.save, snapshot)

    def save(self, record: dict):
        if isinstance(record, LazyRecord):
            record.materialize()
        directory = self.directory(record['id'])
        directory.mkdir(exist_ok=True, mode=0o700)
        if not (directory / 'memory.md').exists():
            (directory / 'memory.md').touch(mode=0o600, exist_ok=True)
        path = directory / 'session.json'
        previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if previous.get('version') == 1 and not (directory / 'session.v1.bak').exists():
            self.atomic_write(directory / 'session.v1.bak', path.read_text(encoding='utf-8'))
        committed = previous.get('message_count', 0) if previous.get('version') == 2 else 0
        offset = previous.get('message_bytes', 0) if previous.get('version') == 2 else 0
        count = record.get('_count', len(record['messages']))
        if count < committed:
            raise ValueError('会话消息只允许追加，拒绝覆盖已提交历史')
        journal = directory / 'messages.jsonl'
        if journal.is_symlink():
            raise ValueError('消息日志不能是符号链接')
        descriptor = os.open(journal, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, 'r+b') as stream:
            if stream.seek(0, os.SEEK_END) < offset:
                raise ValueError('消息日志短于已提交索引，拒绝覆盖')
            stream.truncate(offset)  # 仅清理崩溃留下的未提交尾部。
            stream.seek(offset)
            for index in range(committed, count):
                stream.write((json.dumps(record['messages'][index], ensure_ascii=False) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
            end = stream.tell()
        metadata = {key: value for key, value in record.items() if key != 'messages' and not key.startswith('_')}
        metadata.update(version=2, updated_at=now(), message_count=count, message_bytes=end,
                        system_message=record.get('_system', record['messages'][0]))
        # 日志先 fsync，原子索引后提交；恢复只读取已提交字节。
        self.atomic_write(path, json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')

    @staticmethod
    def read_record(path: Path) -> dict:
        record = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(record, dict) and record.get('version') == 2 and 'messages' not in record:
            journal = path.parent / 'messages.jsonl'
            if journal.is_symlink():
                raise ValueError('消息日志不能是符号链接')
            with journal.open('rb') as stream:
                payload = stream.read(record['message_bytes'])
            if len(payload) != record['message_bytes'] or not payload.endswith(b'\n'):
                raise ValueError('消息日志不完整')
            record['messages'] = [json.loads(line) for line in payload.splitlines()]
            if len(record['messages']) != record['message_count']:
                raise ValueError('消息数量与索引不符')
            record['messages'][0] = record['system_message']
        return record

    def export_saved(self, identifier: str, format: str = 'md') -> Path:
        """调用方先 flush；在磁盘线程读取已提交快照，避免 UI 全量复制。"""
        directory = self.directory(identifier)
        record = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
        if record.get('version') == 2 and 'messages' not in record:
            return self._export_stream(record, self.iter_messages(directory, record), format)
        return self.export(record, format)

    @staticmethod
    def iter_messages(directory: Path, metadata: dict):
        """只迭代提交边界内的行；原子发布前验证长度和消息数量。"""
        journal = directory / 'messages.jsonl'
        if journal.is_symlink():
            raise ValueError('消息日志不能是符号链接')
        remaining = metadata['message_bytes']
        count = 0
        with journal.open('rb') as stream:
            while remaining > 0:
                line = stream.readline(remaining)
                if not line or not line.endswith(b'\n'):
                    raise ValueError('消息日志不完整')
                remaining -= len(line)
                message = json.loads(line)
                yield metadata['system_message'] if count == 0 else message
                count += 1
        if count != metadata['message_count']:
            raise ValueError('消息数量与索引不符')

    def session_bytes(self, identifier: str) -> int:
        directory = self.directory(identifier)
        return sum(p.stat().st_size for p in (directory / 'session.json', directory / 'messages.jsonl') if p.exists())

    @staticmethod
    def atomic_write(path: Path, text: str):
        descriptor, temporary = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def new_record(self, title: str, prompt: str) -> dict:
        record = {'version': 1, 'id': uuid.uuid4().hex, 'title': title,
                  'created_at': now(), 'status': 'idle', 'error': '',
                  'messages': [{'role': 'system', 'content': prompt}]}
        record['updated_at'] = now()
        return record

    def create(self, title: str, prompt: str) -> dict:
        record = self.new_record(title, prompt)
        self.save(record)
        return record

    def load_record(self, path: Path) -> dict:
        record = self.read_record(path)
        if not isinstance(record, dict):
            raise ValueError('会话文件必须是 JSON 对象')
        if (record.get('version') not in {1, 2} or record['id'] != path.parent.name
                or self.directory(record['id']) != path.parent
                or not isinstance(record['messages'], list)
                or not record['messages'] or record['messages'][0]['role'] != 'system'
                or not isinstance(record['title'], str)):
            raise ValueError('无效会话格式')
        for message in record['messages']:
            if not isinstance(message, dict) or message.get('role') not in {'system', 'user', 'assistant', 'tool'}:
                raise ValueError('无效消息角色')
        if record['status'] not in {'idle', 'error', 'cancelled', 'interrupted', 'checkpoint'}:
            record['status'] = 'interrupted'
            record['error'] = '上次任务被中断；已恢复已保存内容，不会自动重放工具。'
            finish_pending_tools(record['messages'])
            self.save(record)
        return record

    def list_metadata(self) -> list[dict]:
        """只读索引；旧版单文件格式仍需解析，访问前不迁移或恢复。"""
        records = []
        for path in sorted(self.root.glob('*/session.json')):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(record, dict) or record.get('version') not in {1, 2}
                        or record['id'] != path.parent.name
                        or self.directory(record['id']) != path.parent
                        or not isinstance(record['title'], str)):
                    raise ValueError('无效会话格式')
                if record['version'] == 2:
                    if (record['message_count'] < 1 or record['message_bytes'] < 1
                            or record['system_message']['role'] != 'system'):
                        raise ValueError('无效消息索引')
                else:
                    if not record['messages'] or record['messages'][0]['role'] != 'system':
                        raise ValueError('无效会话格式')
                    record['message_count'] = len(record['messages'])
                    record.pop('messages')
                records.append(LazyRecord(record, lambda path=path: self.load_record(path)))
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.errors.append(f'{path.parent.name}: {exc}')
        return sorted(records, key=lambda r: r['updated_at'])

    def load_all(self) -> list[dict]:
        """兼容需要全量历史的调用者；交互启动与列表使用 list_metadata。"""
        records = []
        for record in self.list_metadata():
            try:
                records.append(record.materialize())
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.errors.append(f'{record["id"]}: {exc}')
        return records

    def memory_path(self, identifier: str, namespace: str | None = None) -> Path:
        self.directory(identifier)
        if namespace is not None:
            if namespace == 'session':
                # 分支始终拥有独立会话记忆，不受旧 MEMORY_SHARED 的别名影响。
                return self.directory(identifier) / 'memory.md'
            if not config.ENABLE_MEMORY_MANAGEMENT:
                raise ValueError('命名空间需要 ENABLE_MEMORY_MANAGEMENT=true')
            if namespace == 'global':
                return self.root / 'memory' / 'global.md'
            if namespace == 'project':
                key = hashlib.sha256(str(self.workspace_root).encode()).hexdigest()[:16]
                return self.root / 'memory' / f'project-{key}.md'
            raise ValueError('记忆命名空间必须为 session/project/global')
        return self.root / 'shared-memory.md' if config.MEMORY_SHARED else self.directory(identifier) / 'memory.md'

    def memory(self, identifier: str, namespace: str | None = None) -> str:
        path = self.memory_path(identifier, namespace)
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def memory_entries(self, identifier: str, namespace: str | None = None) -> list[dict]:
        from ai_agent_startup.core.memory import parse_entries
        return parse_entries(self.memory(identifier, namespace))

    def memory_for_model(self, identifier: str, query: str = '', namespace: str | None = None) -> str:
        from ai_agent_startup.core.memory import parse_entries, select_memory
        path = self.memory_path(identifier, namespace)
        if not path.exists():
            return ''
        with path.open(encoding='utf-8') as stream:
            text = stream.read(config.MEMORY_MAX_CHARS * 8)
        return select_memory(parse_entries(text), query)

    def remember(self, identifier: str, text: str, source: str = 'manual', tags=None, *, namespace=None, expires_at=None):
        from ai_agent_startup.core.memory import render_entries, MemoryPatch
        text = text.strip()
        if not text:
            raise ValueError('记忆不能为空')
        with self._memory_lock:
            entries = self.memory_entries(identifier, namespace)
            if any(entry['text'].strip() == text for entry in entries):
                return
            if sum(len(entry['text']) for entry in entries) + len(text) > config.MEMORY_MAX_CHARS:
                raise ValueError('记忆超过 MEMORY_MAX_CHARS，请先整理或删除旧记忆')
            if len(entries) >= 64:
                raise ValueError('记忆最多 64 条，请先删除不再需要的条目')
            entry = {'id': uuid.uuid4().hex[:12], 'text': text, 'created_at': now(), 'source': source, 'tags': tags or []}
            if config.ENABLE_MEMORY_MANAGEMENT:
                entry.update(MemoryPatch(text=text, source=source, tags=tags or [], expires_at=expires_at).model_dump(mode='json', exclude_none=True))
            elif expires_at is not None:
                raise ValueError('到期管理需要 ENABLE_MEMORY_MANAGEMENT=true')
            entries.append(entry)
            if config.ENABLE_MEMORY_MANAGEMENT:
                self._validate_memory_entries(entries)
            path = self.memory_path(identifier, namespace)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.atomic_write(path, render_entries(entries))
            if config.ENABLE_MEMORY_MANAGEMENT:
                self._audit_memory(identifier, 'memory_add', namespace, entry['id'])

    def remove_memory(self, identifier: str, entry_id: str, namespace=None):
        from ai_agent_startup.core.memory import render_entries
        with self._memory_lock:
            entries = self.memory_entries(identifier, namespace)
            remaining = [entry for entry in entries if entry['id'] != entry_id]
            if len(remaining) == len(entries):
                raise ValueError('记忆 ID 不存在')
            self.atomic_write(self.memory_path(identifier, namespace), render_entries(remaining))
            if config.ENABLE_MEMORY_MANAGEMENT:
                self._audit_memory(identifier, 'memory_remove', namespace, entry_id)

    def _validate_memory_entries(self, entries):
        from ai_agent_startup.core.memory import render_entries
        if (len(entries) > 64 or sum(len(entry['text']) for entry in entries) > config.MEMORY_MAX_CHARS
                or len(render_entries(entries)) > config.MEMORY_MAX_CHARS * 8):
            raise ValueError('记忆超过 64 条或 MEMORY_MAX_CHARS/元数据上限；请显式整理旧条目')

    def _audit_memory(self, identifier, event, namespace, entry_id):
        from ai_agent_startup.core.audit_writer import append_audit
        from ai_agent_startup.core.log import get_logger
        try:
            append_audit(self.directory(identifier) / 'audit.jsonl', json.dumps({
                'ts': now(), 'event': event, 'session_id': identifier,
                'namespace': namespace or 'default', 'entry_id': entry_id}, ensure_ascii=False), sync=config.AUDIT_SYNC)
        except OSError as exc:
            get_logger(__name__).warning('记忆审计写入失败：%s', exc)

    def search_memory(self, identifier, query='', *, tags=None, source=None, include_expired=False, namespace=None):
        from ai_agent_startup.core.memory import filter_entries, MemoryQuery, BM25Index
        if not config.ENABLE_MEMORY_MANAGEMENT:
            raise ValueError('请显式开启 ENABLE_MEMORY_MANAGEMENT')
        filters = MemoryQuery(query=query, tags=tags or [], source=source, include_expired=include_expired)
        entries = filter_entries(self.memory_entries(identifier, namespace), tags=filters.tags,
                                 source=filters.source, include_expired=filters.include_expired)
        if not filters.query:
            return entries
        ranking = BM25Index([entry['text'] for entry in entries]).search(filters.query, len(entries))
        return [{**entries[index], 'score': score} for index, score in ranking]

    def edit_memory(self, identifier, entry_id, patch: dict, *, namespace=None):
        from ai_agent_startup.core.memory import MemoryPatch, render_entries
        if not config.ENABLE_MEMORY_MANAGEMENT:
            raise ValueError('请显式开启 ENABLE_MEMORY_MANAGEMENT')
        changes = MemoryPatch.model_validate(patch).model_dump(mode='json', exclude_unset=True)
        if not changes:
            raise ValueError('修改内容不能为空')
        with self._memory_lock:
            entries = self.memory_entries(identifier, namespace)
            for entry in entries:
                if entry['id'] == entry_id:
                    entry.update(changes, updated_at=now())
                    break
            else:
                raise ValueError('记忆 ID 不存在')
            self._validate_memory_entries(entries)
            self.atomic_write(self.memory_path(identifier, namespace), render_entries(entries))
            self._audit_memory(identifier, 'memory_edit', namespace, entry_id)

    def clear_memory(self, identifier, namespace=None):
        with self._memory_lock:
            path = self.memory_path(identifier, namespace)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.atomic_write(path, '')
            if config.ENABLE_MEMORY_MANAGEMENT:
                self._audit_memory(identifier, 'memory_clear', namespace, '')

    def export(self, record: dict, format: str = 'md') -> Path:
        return self._export_stream(record, iter(record['messages']), format)

    def _export_stream(self, record, messages, format):
        if format not in {'md', 'json'}:
            raise ValueError('导出格式必须是 md 或 json')
        directory = self.root / 'exports'
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / f'{record["id"]}-{uuid.uuid4().hex[:8]}.{format}'
        descriptor, temporary = tempfile.mkstemp(prefix='.writing-', dir=directory)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                if format == 'json':
                    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
                    def write_value(value, indent):
                        for chunk in encoder.iterencode(value):
                            stream.write(chunk.replace('\n', '\n' + ' ' * indent))
                    stream.write('{\n')
                    keys = [key for key in record if key != 'memory']
                    if 'messages' not in keys:
                        keys.append('messages')
                    keys.append('memory')
                    for i, key in enumerate(keys):
                        if i:
                            stream.write(',\n')
                        stream.write('  ' + json.dumps(key, ensure_ascii=False) + ': ')
                        if key == 'messages':
                            stream.write('[')
                            seen = False
                            for message in messages:
                                stream.write(',\n    ' if seen else '\n    ')
                                write_value(message, 4)
                                seen = True
                            stream.write('\n  ]' if seen else ']')
                        else:
                            write_value(self.memory(record['id'], record.get('memory_namespace')) if key == 'memory' else record[key], 2)
                    stream.write('\n}')
                else:
                    stream.write(f'# {record["title"]}\n\n会话：{record["id"]}\n\n## 记忆\n')
                    stream.write(self.memory(record['id'], record.get('memory_namespace')))
                    for message in messages:
                        stream.write('\n\n## ' + message['role'] + '\n' + str(message.get('content') or ''))
                        for reference in message.get('_attachments', []):
                            stream.write('\n图片附件引用：' + str(reference.get('sha256', '')) + '.jpg（附件目录单独保存）')
                        if message.get('tool_calls'):
                            stream.write('\n```json\n')
                            json.dump(message['tool_calls'], stream, ensure_ascii=False, indent=2)
                            stream.write('\n```')
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path
