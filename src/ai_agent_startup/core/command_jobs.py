"""显式启用的持久命令任务；工作区写入互斥，重启只标记中断，绝不重放。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import copy_context
from dataclasses import replace
import json
import math
import os
import re
import threading
import time
import uuid

from ai_agent_startup import config
from ai_agent_startup.tools.sandbox import (
    _context, tool_context, check_command, isolated_command, check_workspace_quota,
    ask_permission, audit, SandboxError,
)


class CommandJobs:
    def __init__(self, store):
        self.store = store
        self.directory = store.root / 'command-jobs'
        if self.directory.is_symlink():
            raise ValueError('任务目录不能是符号链接')
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.workspace_locks = {}
        self.records = {}
        self.events = {}
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=config.COMMAND_JOB_CONCURRENCY, thread_name_prefix='command-job')
        for path in self.directory.glob('*.json'):
            if path.is_symlink() or not re.fullmatch('[a-f0-9]{32}', path.stem):
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            if record['status'] in {'queued', 'running'}:
                record['status'] = 'interrupted'
                record['result'] = '上次进程中断，执行结果未知，请人工检查工作区；不会自动重放。'
                self.store.atomic_write(path, json.dumps(record, ensure_ascii=False))
            self.records[path.stem] = record

    @contextmanager
    def guard(self, root, cancelled):
        with self.lock:
            lock = self.workspace_locks.setdefault(str(root.resolve()), threading.Lock())
        while not lock.acquire(timeout=.05):
            if cancelled.is_set():
                raise SandboxError('等待后台命令期间已取消')
        try:
            if cancelled.is_set():
                raise SandboxError('任务已取消')
            yield
        finally:
            lock.release()

    def _save(self, identifier):
        self.store.atomic_write(self.directory / (identifier + '.json'), json.dumps(self.records[identifier], ensure_ascii=False))

    def start(self, command, reason='', network=False, timeout=None):
        context = _context.get()
        if not config.ENABLE_COMMAND_JOBS or context is None or context.job_manager is not self:
            raise PermissionError('后台命令未启用或无会话上下文')
        timeout = config.COMMAND_JOB_MAX_SECONDS if timeout is None else timeout
        if not math.isfinite(timeout) or not 0 < timeout <= config.COMMAND_JOB_MAX_SECONDS:
            raise ValueError('任务超时必须为正数且不超过 COMMAND_JOB_MAX_SECONDS')
        check_workspace_quota()
        argv = check_command(command)
        isolated_command(argv)  # 在确认前探测能力，不创建宿主进程。
        if network:
            from ai_agent_startup.tools.network_proxy import validate_network_config
            validate_network_config()
            if config.COMMAND_NETWORK != 'allowlist':
                raise PermissionError('联网命令未启用')
        # 长任务始终明确确认持续时间；不从原 run_command 短任务授权推导。
        detail = f'{command}\n后台时限 {timeout:g}s；联网={network}'
        if network:
            detail += f'；白名单={config.COMMAND_NETWORK_ALLOWLIST}'
        if not ask_permission('start_command_job', detail, reason, force_confirmation=True):
            raise PermissionError('用户未确认后台命令')
        with self.lock:
            if self.closed or context.cancelled.is_set():
                raise PermissionError('任务管理器已关闭或当前调用已取消')
            if sum(r['status'] in {'queued', 'running'} for r in self.records.values()) >= config.COMMAND_JOB_MAX_ACTIVE:
                raise ValueError('后台任务队列已满')
            identifier = uuid.uuid4().hex
            cancelled = threading.Event()
            self.records[identifier] = {'id': identifier, 'owner': context.root.name, 'command': command,
                'network': network, 'timeout': timeout, 'status': 'queued', 'created_at': time.time(), 'result': ''}
            self.events[identifier] = cancelled
            self._save(identifier)
            job_context = replace(context, cancelled=cancelled)
            self.executor.submit(copy_context().run, self._run, identifier, job_context, reason)
            audit('command_job_queued', job_id=identifier, timeout=timeout, network=network)
            return identifier

    def _run(self, identifier, context, reason):
        from ai_agent_startup.tools.command import _execute
        from ai_agent_startup.tools.network_proxy import NetworkProxy
        with tool_context(context):
            record = self.records[identifier]
            try:
                with self.guard(context.root, context.cancelled):
                    with self.lock:
                        record['status'] = 'running'
                        self._save(identifier)
                    path = self.directory / (identifier + '.log')
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                    with os.fdopen(descriptor, 'wb', buffering=0) as stream:
                        size = 0
                        def on_output(data):
                            nonlocal size
                            retained = data[:max(0, config.COMMAND_JOB_LOG_BYTES - size)]
                            if retained:
                                size += stream.write(retained)
                        with NetworkProxy() if record['network'] else nullcontext() as proxy:
                            argv = check_command(record['command'])
                            isolated = isolated_command(argv, network_socket=proxy.path) if proxy else isolated_command(argv)
                            result = _execute(record['command'], reason, argv, isolated,
                                              timeout=record['timeout'], on_output=on_output)
                    status = ('cancelled' if context.cancelled.is_set() else 'timeout' if result.startswith('[超时]')
                              else 'completed' if result.startswith('[退出码] 0\n') else 'failed')
            except Exception as exc:
                result = f'{type(exc).__name__}: {exc}'
                status = 'cancelled' if context.cancelled.is_set() else 'failed'
            finally:
                with self.lock:
                    record.update(status=status, result=result, finished_at=time.time())
                    self._save(identifier)
                    self.events.pop(identifier, None)
                audit('command_job_finished', job_id=identifier, status=status)

    def status(self, owner, identifier):
        with self.lock:
            record = self.records.get(identifier)
            if record is None or record['owner'] != owner:
                raise PermissionError('任务不存在或不属于当前会话')
            return dict(record)

    def logs(self, owner, identifier):
        self.status(owner, identifier)
        path = self.directory / (identifier + '.log')
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return ''
        with os.fdopen(descriptor, 'rb') as stream:
            return stream.read(config.COMMAND_JOB_LOG_BYTES).decode('utf-8', errors='replace')

    def cancel(self, owner, identifier):
        with self.lock:
            self.status(owner, identifier)
            if identifier in self.events:
                self.events[identifier].set()
        audit('command_job_cancel_requested', job_id=identifier, owner=owner)

    def stop(self):
        with self.lock:
            self.closed = True
            for event in self.events.values():
                event.set()

    def close(self):
        self.stop()
        self.executor.shutdown(wait=True)
