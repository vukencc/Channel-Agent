"""项目工作区授权绑定，与私有会话状态和上下文分开保存。"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import stat
import threading
from pathlib import Path

from agent_core import config

from agent_core.tools.sandbox import SandboxError


@dataclass(frozen=True)
class WorkspaceBinding:
    parent_id: str | None
    owner_id: str
    mode: str
    path: str | None = None
    identity: tuple[int, int] | None = None


class SessionWorkspaces:
    def __init__(self, manager):
        self.manager = manager
        self.bindings = {session.id: self._binding(session.record)
                         for session in manager.sessions.values()}
        self.lock = threading.Lock()
        self.workspace_locks = {}

    @staticmethod
    def _binding(record):
        identity = record.get('workspace_identity')
        if isinstance(identity, (list, tuple)):
            identity = tuple(identity)
        return WorkspaceBinding(record.get('delegated_from') or None,
            record.get('workspace_owner_id', record['id']), record.get('workspace_mode', 'isolated'),
            record.get('workspace_path'), identity)

    def protected_paths(self):
        """应用自身的源码、凭据、状态、知识库与只读根不能被授予写权限。"""
        return [Path(path).resolve() for path in (
            config.PROJECT_ROOT, self.manager.store.root, self.manager.store.workspace_root,
            config.AGENT_STATE_DIR, config.AUDIT_LOG, config.DOC_DIR, config.RAG_CACHE_DIR,
            *(entry['path'] for entry in config.TOOL_ROOTS.values()),
            *(entry['path'] for entry in config.RAG_SOURCES.values()))]

    @staticmethod
    def directory_identity(path: Path):
        """沿目录句柄逐级打开，拒绝祖先或末级的符号链接与竞态替换。"""
        if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
            raise PermissionError('当前平台不支持安全的 Linux 工作区目录选择')
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('工作区必须为已有的 Linux 绝对目录')
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parts[1:]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            info = os.fstat(descriptor)
            if path.resolve(strict=True) != path:
                raise PermissionError('工作区目录不能包含符号链接')
            current = path.stat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise PermissionError('工作区目录在验证期间发生变化')
            return info.st_dev, info.st_ino
        except OSError as exc:
            raise PermissionError('工作区必须为可访问的已有目录，且不能包含符号链接') from exc
        finally:
            os.close(descriptor)

    def validate_path(self, value):
        if value is None or value == '':
            return None, None
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError('工作区路径无效')
        path = Path(value)
        identity = self.directory_identity(path)
        if any(part in {'.agent', '.ssh', '.gnupg', '.aws', '.codex'} for part in path.parts):
            raise PermissionError('不能选择应用状态或用户私有目录')
        if Path.home().resolve().is_relative_to(path) or path in {
                Path('/tmp'), Path('/var'), Path('/mnt'), Path('/media'), Path('/opt'), Path('/srv')}:
            raise PermissionError('请选择具体项目目录，不能选择系统或家目录根')
        if any(path.is_relative_to(Path(base)) for base in (
                '/etc', '/usr', '/bin', '/sbin', '/lib', '/lib64', '/dev', '/proc', '/sys', '/boot', '/run', '/root')):
            raise PermissionError('不能选择系统目录')
        if any(path.is_relative_to(protected) or protected.is_relative_to(path)
               for protected in self.protected_paths()):
            raise PermissionError('工作区不能与应用源码、凭据、状态或只读数据目录重叠')
        return str(path), identity

    def owner_binding(self, session):
        binding = self.binding(session)
        return self._parent_binding(binding.owner_id) if binding.owner_id != session.id else binding

    def identity(self, session):
        return self.owner_binding(session).identity

    def path(self, session):
        binding = self.owner_binding(session)
        if binding.path is None:
            if binding.identity is not None:
                raise PermissionError('工作区授权记录无效')
            return self.manager.store.workspace(binding.owner_id)
        try:
            path, identity = self.validate_path(binding.path)
            if (path is None or not isinstance(binding.identity, tuple)
                    or len(binding.identity) != 2 or identity != binding.identity):
                raise PermissionError('工作区目录身份已变化，请由用户重新选择目录')
            return Path(path)
        except (OSError, ValueError) as exc:
            raise PermissionError('已选择的工作区不可用，请由用户重新选择目录') from exc

    def can_change(self, session):
        try:
            binding = self.binding(session)
        except (PermissionError, ValueError):
            return False
        if binding.parent_id is not None:
            return False
        if self.manager.closing or self.manager.maintenance or any(
            value.busy or value.tool_workers for value in self.manager.sessions.values()
            if self.bindings.get(value.id) and self.bindings[value.id].owner_id == session.id):
            return False
        queue = self.manager.agent_tasks
        if queue is not None:
            for identifier, task in queue.records.items():
                runner = queue.tasks.get(identifier)
                if task.get('owner') == session.id and (
                        task.get('status') in {'queued', 'running'}
                        or (runner is not None and not runner.done())):
                    return False
        jobs = self.manager.command_jobs
        if jobs is not None:
            with jobs.lock:
                for identifier, job in jobs.records.items():
                    owner = self.bindings.get(job.get('owner'))
                    future = jobs.futures.get(identifier)
                    if owner is not None and owner.owner_id == session.id and (
                            job.get('status') in {'queued', 'running', 'waiting_workspace'}
                            or (future is not None and not future.done())):
                        return False
        return True

    def selected_paths(self):
        """维护保护保留失效路径与回收区记录，不能依赖可成功打开的工作区。"""
        values = [binding.path for binding in self.bindings.values() if binding.path is not None]
        trash = self.manager.store.root / '.trash'
        if trash.is_symlink():
            raise PermissionError('工作区回收记录不能是符号链接')
        for path in trash.glob('*/session.json'):
            if path.is_symlink() or path.parent.is_symlink():
                raise PermissionError('工作区回收记录不能是符号链接')
            self.manager.store.directory(path.parent.name)
            with path.open(encoding='utf-8') as stream:
                record = json.load(stream)
            if not isinstance(record, dict) or record.get('id') != path.parent.name:
                raise PermissionError('工作区回收记录无效，拒绝清理')
            if record.get('workspace_path') is not None:
                values.append(record['workspace_path'])
        if any(not isinstance(value, str) or not Path(value).is_absolute() for value in values):
            raise PermissionError('工作区保护记录无效，拒绝清理')
        return [Path(value) for value in values]

    def set_path(self, session, value):
        binding = self.binding(session)
        if binding.parent_id is not None:
            raise PermissionError('子 Agent 继承根会话工作区，不能自行切换')
        if not self.can_change(session):
            raise ValueError('请等待共享工作区的所有 Agent、工具和后台命令结束再切换')
        path, identity = self.validate_path(value)
        session.record['workspace_path'] = path
        session.record['workspace_identity'] = list(identity) if identity else None
        self.register(session)

    def metadata(self, session):
        """状态流不创建默认目录，失效工作区仍可显示并让用户重新选择。"""
        path, custom = '', False
        try:
            binding = self.owner_binding(session)
            path = binding.path or str(self.manager.store.workspace_path(binding.owner_id))
            custom = binding.path is not None
            if binding.path is not None:
                self.path(session)
            return {'workspace_path': path, 'workspace_custom': custom,
                    'workspace_can_change': self.can_change(session), 'workspace_error': ''}
        except (ValueError, PermissionError, OSError) as exc:
            return {'workspace_path': path, 'workspace_custom': custom,
                    'workspace_can_change': self.can_change(session), 'workspace_error': str(exc)}

    def directories(self, value=None):
        """仅供本机用户浏览目录；该结果不改变任何 Agent 的授权。"""
        path = Path(value) if value else Path.home()
        self.directory_identity(path)
        protected = self.protected_paths()
        private = {'.agent', '.ssh', '.gnupg', '.aws', '.codex'}
        if any(part in private for part in path.parts) or any(path.is_relative_to(item) for item in protected):
            raise PermissionError('不能浏览应用或用户私有目录')
        from agent_core.core.files import opened_path
        directories = []
        truncated = False
        with opened_path(Path('/'), path, directory=True) as descriptor:
            with os.scandir(descriptor) as scan:
                for item in scan:
                    target = path / item.name
                    if item.name.startswith('.') or item.is_symlink() or not item.is_dir(follow_symlinks=False):
                        continue
                    if any(target.is_relative_to(entry) for entry in protected):
                        continue
                    if len(directories) >= 200:
                        truncated = True
                        break
                    directories.append({'name': item.name, 'path': str(target)})
        error = ''
        try:
            self.validate_path(str(path))
        except (ValueError, PermissionError, OSError) as exc:
            error = str(exc)
        return {'path': str(path), 'parent_path': str(path.parent) if path.parent != path else None,
                'directories': sorted(directories, key=lambda item: item['name'].casefold()),
                'truncated': truncated, 'selectable': not error, 'error': error}

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
        if binding.parent_id is not None and (binding.path is not None or binding.identity is not None):
            raise PermissionError('子 Agent 不能指定宿主工作区')
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
        self.register(session)

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
