"""Atomic, human-readable session files. One CLI owns a state directory at a time."""
import asyncio
import copy
import concurrent.futures
import shutil
import threading

import config
import fcntl
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                         'content': '[中断] 执行结果未知，请先检查文件状态，勿自动重放操作。'})


class SessionStore:
    def __init__(self, root: Path, workspace_root: Path | None = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / '.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('该状态目录已有 CLI 在运行；请使用不同的 --state-dir') from None
        self.errors: list[str] = []
        self.workspace_root = Path(workspace_root or config.SANDBOX_DIR).resolve()
        self.writer = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-save")
        self._workspace_lock = threading.Lock()

    def close(self):
        self.writer.shutdown(wait=True)
        if not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
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
        record['updated_at'] = now()
        snapshot = copy.deepcopy({key: value for key, value in record.items() if key != 'messages'})
        snapshot['messages'] = record['messages']
        snapshot['_count'] = len(record['messages'])
        snapshot['_system'] = copy.deepcopy(record['messages'][0])
        return asyncio.get_running_loop().run_in_executor(self.writer, self.save, snapshot)

    def save(self, record: dict):
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
        return self.export(self.read_record(self.directory(identifier) / 'session.json'), format)

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

    def load_all(self) -> list[dict]:
        records = []
        for path in sorted(self.root.glob('*/session.json')):
            try:
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
                records.append(record)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.errors.append(f'{path.parent.name}: {exc}')
        return sorted(records, key=lambda r: r['updated_at'])

    def memory(self, identifier: str) -> str:
        path = self.directory(identifier) / 'memory.md'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def memory_for_model(self, identifier: str) -> str:
        path = self.directory(identifier) / 'memory.md'
        if not path.exists():
            return ''
        with path.open(encoding='utf-8') as stream:
            text = stream.read(config.MEMORY_MAX_CHARS + 1)
        return text if len(text) <= config.MEMORY_MAX_CHARS else text[:config.MEMORY_MAX_CHARS] + '\n[记忆超限，注入已截断；原文件保留]'

    def remember(self, identifier: str, text: str):
        path = self.directory(identifier) / 'memory.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        updated = self.memory(identifier).rstrip() + '\n- ' + text.strip() + '\n'
        if len(updated) > config.MEMORY_MAX_CHARS:
            raise ValueError('记忆超过 MEMORY_MAX_CHARS，请先整理或删除旧记忆')
        self.atomic_write(path, updated)

    def export(self, record: dict, format: str = 'md') -> Path:
        if format not in {'md', 'json'}:
            raise ValueError('导出格式必须是 md 或 json')
        directory = self.root / 'exports'
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / f'{record["id"]}-{uuid.uuid4().hex[:8]}.{format}'
        if format == 'json':
            text = json.dumps({**record, 'memory': self.memory(record['id'])}, ensure_ascii=False, indent=2)
        else:
            lines = [f'# {record["title"]}', '', f'会话：{record["id"]}', '', '## 记忆', self.memory(record['id'])]
            for message in record['messages']:
                lines += ['', '## ' + message['role'], str(message.get('content') or '')]
                if message.get('tool_calls'):
                    lines += ['```json', json.dumps(message['tool_calls'], ensure_ascii=False, indent=2), '```']
            text = '\n'.join(lines)
        self.atomic_write(path, text + '\n')
        return path
