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
        """Snapshot on the event loop; serialize and fsync on one ordered writer."""
        record['updated_at'] = now()
        snapshot = copy.deepcopy(record)
        return asyncio.get_running_loop().run_in_executor(self.writer, self.save, snapshot)

    def save(self, record: dict):
        directory = self.directory(record['id'])
        directory.mkdir(exist_ok=True, mode=0o700)
        record['updated_at'] = now()
        if not (directory / 'memory.md').exists():
            (directory / 'memory.md').touch(mode=0o600, exist_ok=True)
        self.atomic_write(directory / 'session.json', json.dumps(record, ensure_ascii=False, indent=2) + '\n')

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
                record = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(record, dict):
                    raise ValueError('会话文件必须是 JSON 对象')
                if (record.get('version') != 1 or record['id'] != path.parent.name
                        or self.directory(record['id']) != path.parent
                        or not isinstance(record['messages'], list)
                        or not record['messages'] or record['messages'][0]['role'] != 'system'
                        or not isinstance(record['title'], str)):
                    raise ValueError('无效会话格式')
                for message in record['messages']:
                    if not isinstance(message, dict) or message.get('role') not in {'system', 'user', 'assistant', 'tool'}:
                        raise ValueError('无效消息角色')
                if record['status'] not in {'idle', 'error', 'cancelled', 'interrupted'}:
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

    def remember(self, identifier: str, text: str):
        path = self.directory(identifier) / 'memory.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.atomic_write(path, self.memory(identifier).rstrip() + '\n- ' + text.strip() + '\n')

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
