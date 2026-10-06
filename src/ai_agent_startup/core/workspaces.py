"""项目工作区授权绑定，与私有会话状态和上下文分开保存。"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import stat
import threading

from ai_agent_startup.tools.sandbox import SandboxError


@dataclass(frozen=True)
class WorkspaceBinding:
    parent_id: str | None
    owner_id: str
    mode: str


class SessionWorkspaces:
    def __init__(self, manager):
        self.manager = manager
        self.bindings = {session.id: self._binding(session.record)
                         for session in manager.sessions.values()}
        self.lock = threading.Lock()
        self.workspace_locks = {}

    @staticmethod
    def _binding(record):
        return WorkspaceBinding(record.get('delegated_from') or None,
            record.get('workspace_owner_id', record['id']), record.get('workspace_mode', 'isolated'))

    @staticmethod
    def validate_mode(mode: str) -> str:
        if not isinstance(mode, str) or mode not in {'isolated', 'shared'}:
            raise ValueError('工作区模式必须为 isolated/shared')
        return mode

    def register(self, session):
        self.bindings[session.id] = self._binding(session.record)

    def _parent_binding(self, identifier):
        if not isinstance(identifier, str):
            raise PermissionError('无效工作区授权会话 ID')
        self.manager.store.directory(identifier)
        if identifier in self.manager.sessions:
            session = self.manager.sessions[identifier]
            binding = self.bindings.get(identifier)
            if binding is None or session.deleting or self._binding(session.record) != binding:
                raise PermissionError('工作区授权绑定已被修改')
            self.validate_mode(binding.mode)
            return binding
        trash = self.manager.store.root / '.trash'
        directory = trash / identifier
        path = directory / 'session.json'
        if trash.is_symlink() or directory.is_symlink() or path.is_symlink():
            raise PermissionError('工作区授权记录不能是符号链接')
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                 | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(descriptor, encoding='utf-8') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise PermissionError('工作区授权记录必须为普通文件')
                record = json.load(stream)
        except (OSError, ValueError) as exc:
            raise PermissionError('共享工作区缺少父会话授权记录') from exc
        if not isinstance(record, dict) or record.get('id') != identifier:
            raise PermissionError('工作区授权会话 ID 不匹配')
        binding = self._binding(record)
        previous = self.bindings.get(identifier)
        if previous is not None and binding != previous:
            raise PermissionError('工作区授权绑定已被修改')
        return binding

    def binding(self, session) -> WorkspaceBinding:
        if self.manager.sessions.get(session.id) is not session or session.deleting:
            raise PermissionError('会话已删除或正在删除')
        binding = self.bindings.get(session.id)
        if binding is None or self._binding(session.record) != binding:
            raise PermissionError('工作区绑定不能由会话记录自行更改')
        self.validate_mode(binding.mode)
        if not isinstance(binding.owner_id, str):
            raise PermissionError('无效工作区所有者 ID')
        self.manager.store.directory(binding.owner_id)
        if binding.parent_id is None:
            if binding.owner_id != session.id:
                raise PermissionError('根会话只能访问自己的项目工作区')
        elif binding.mode == 'shared':
            parent = self._parent_binding(binding.parent_id)
            if (parent.parent_id is not None or parent.owner_id != binding.parent_id
                    or binding.owner_id != binding.parent_id):
                raise PermissionError('共享工作区仅允许直接父子 Agent')
        elif binding.owner_id != session.id:
            raise PermissionError('独立子会话只能访问自己的工作区')
        return binding

    def set_mode(self, session, mode):
        binding = self.binding(session)
        if binding.parent_id is not None:
            raise PermissionError('子 Agent 不能更改工作区模式')
        mode = self.validate_mode(mode)
        session.record['workspace_mode'] = mode
        session.record['workspace_owner_id'] = binding.owner_id
        self.bindings[session.id] = WorkspaceBinding(None, binding.owner_id, mode)

    @contextmanager
    def guard(self, root, cancelled):
        with self.lock:
            lock = self.workspace_locks.setdefault(str(root.resolve()), threading.Lock())
        while not lock.acquire(timeout=.05):
            if cancelled.is_set():
                raise SandboxError('等待工作区操作期间已取消')
        try:
            if cancelled.is_set():
                raise SandboxError('工作区操作已取消')
            yield
        finally:
            lock.release()
