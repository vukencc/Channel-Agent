"""持久命令任务：沙箱写入互斥，本机允许并发服务；重启不重放。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import copy_context
from dataclasses import replace
import json
import math
import os
import re
import stat
import threading
import time
import uuid

from agent_core import config
from agent_core.tools.sandbox import (
    _context, tool_context, check_command, isolated_command, check_workspace_quota,
    ask_permission, audit, SandboxError, command_memory_limit_mb, execution_environment,
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
        self.futures = {}
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

    def start(self, command, reason='', network=False, timeout=None, runtime_profile='system'):
        context = _context.get()
        mode = execution_environment()
        local = mode == 'local'
        if (not config.ENABLE_COMMAND_JOBS and not local) or context is None or context.job_manager is not self:
            raise PermissionError('后台命令未启用或无会话上下文')
        maximum = config.LOCAL_COMMAND_TIMEOUT if local else config.COMMAND_JOB_MAX_SECONDS
        timeout = maximum if timeout is None else timeout
        if not math.isfinite(timeout) or not 0 < timeout <= maximum:
            raise ValueError('任务超时必须为正数且不超过 ' + ('LOCAL_COMMAND_TIMEOUT' if local else 'COMMAND_JOB_MAX_SECONDS'))
        if local:
            from agent_core.tools.execution_environment import validate_local_execution
            validate_local_execution()
            if runtime_profile not in {'system', 'node'}:
                raise SandboxError('未知命令运行时档位，仅支持 system/node')
        else:
            check_workspace_quota()
        argv = check_command(command)
        if not local:
            isolated_command(argv, runtime_profile=runtime_profile)  # 在确认前探测能力，不创建宿主进程。
        memory_mb = None if local else command_memory_limit_mb(runtime_profile)
        if network and not local:
            from agent_core.tools.network_proxy import validate_network_config
            validate_network_config()
            if config.COMMAND_NETWORK != 'allowlist':
                raise PermissionError('联网命令未启用')
        # 长任务始终明确确认持续时间；不从原 run_command 短任务授权推导。
        detail = f'{command}\n后台时限 {timeout:g}s；联网={network}'
        detail += (f'\n执行环境：Linux 本机；工作目录：{context.root}；本机网络含 localhost；不施加沙箱资源限额'
                   if local else f'\n运行时档位：{runtime_profile}，虚拟地址空间上限：{memory_mb} MiB')
        if network and not local:
            detail += f'；白名单={config.COMMAND_NETWORK_ALLOWLIST}'
        if not ask_permission('start_command_job', detail, reason, command=command, force_confirmation=True):
            raise PermissionError('用户未确认后台命令')
        if local:
            validate_local_execution()
        with self.lock:
            if self.closed or context.cancelled.is_set():
                raise PermissionError('任务管理器已关闭或当前调用已取消')
            if sum(r['status'] in {'queued', 'running'} for r in self.records.values()) >= config.COMMAND_JOB_MAX_ACTIVE:
                raise ValueError('后台任务队列已满')
            identifier = uuid.uuid4().hex
            cancelled = threading.Event()
            self.records[identifier] = {'id': identifier, 'owner': context.session_id or context.root.name, 'command': command,
                'network': network, 'timeout': timeout, 'runtime_profile': runtime_profile,
                'execution_environment': mode,
                'status': 'queued', 'created_at': time.time(), 'result': ''}
            self.events[identifier] = cancelled
            self._save(identifier)
            job_context = replace(context, cancelled=cancelled, execution_environment=mode)
            future = self.executor.submit(copy_context().run, self._run, identifier, job_context, reason)
            self.futures[identifier] = future
            future.add_done_callback(lambda finished: self._finished(identifier, finished))
            audit('command_job_queued', job_id=identifier, timeout=timeout, network=network,
                  runtime_profile=runtime_profile, memory_mb=memory_mb, execution_environment=mode)
            return identifier

    def _finished(self, identifier, future):
        with self.lock:
            if self.futures.get(identifier) is future:
                self.futures.pop(identifier)

    def _run(self, identifier, context, reason):
        from agent_core.tools.command import _execute, _network_diagnostics
        from agent_core.tools.network_proxy import NetworkProxy
        record = self.records[identifier]
        mode = record.get('execution_environment', 'sandbox')
        local = mode == 'local'
        context = replace(context, execution_environment=mode)
        with tool_context(context):
            runtime_profile = record.get('runtime_profile', 'system')
            try:
                # 本机服务需与前台探测/编辑并发，不能在整个服务生命周期独占项目锁。
                # 只在启动校验时短暂持锁；沙箱继续使用原有整段互斥。
                if local:
                    from agent_core.tools.execution_environment import validate_local_execution
                    with self.guard(context.root, context.cancelled):
                        validate_local_execution()
                with nullcontext() if local else self.guard(context.root, context.cancelled):
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
                        with NetworkProxy() if record['network'] and not local else nullcontext() as proxy:
                            argv = check_command(record['command'])
                            options = {}
                            if local:
                                from agent_core.tools.execution_environment import local_process_environment, validate_local_execution
                                validate_local_execution()
                                isolated = argv
                                options = {'environment': local_process_environment(), 'check_quota': False,
                                           'execution_mode': 'local'}
                            else:
                                isolated = (isolated_command(argv, network_socket=proxy.path, runtime_profile=runtime_profile)
                                            if proxy else isolated_command(argv, runtime_profile=runtime_profile))
                            result = _execute(record['command'], reason, argv, isolated,
                                              timeout=record['timeout'], on_output=on_output, **options)
                            result += _network_diagnostics(proxy)
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
                audit('command_job_finished', job_id=identifier, status=status, runtime_profile=runtime_profile,
                      execution_environment=mode)

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

    def inspect(self, owner: str, identifier: str) -> dict:
        """一次检查状态与已保留日志尾部，按实际 JSON 长度限制返回。"""
        with self.lock:
            record = self.status(owner, identifier)
            future = self.futures.get(identifier)
            drained = record['status'] not in {'queued', 'running'} and (future is None or future.done())
        result = {'job_id': identifier, 'status': record['status'], 'drained': drained,
                  'runtime_profile': record.get('runtime_profile', 'system'),
                  'execution_environment': record.get('execution_environment', 'sandbox'),
                  'result': '', 'result_truncated': False, 'log_tail': '',
                  'log_truncated': False, 'log_capture_limit_reached': False}
        maximum = config.TOOL_MAX_OUTPUT
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        if len(encode(result)) > maximum:
            raise ValueError('TOOL_MAX_OUTPUT 太小，无法返回完整后台命令身份与状态')
        path = self.directory / (identifier + '.log')
        size = 0
        data = b''
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('后台命令日志必须是普通文件')
                size = info.st_size
                offset = max(0, size - min(config.COMMAND_JOB_LOG_BYTES, maximum * 4))
                stream.seek(offset)
                data = stream.read(min(config.COMMAND_JOB_LOG_BYTES, maximum * 4))
                # 从 UTF-8 字符中间 seek 时跳过残缺前缀；日志内部非法字节仍显式替换。
                if offset:
                    while data and data[0] & 0xc0 == 0x80:
                        data = data[1:]
        texts = {'result': str(record.get('result', '') or ''),
                 'log_tail': data.decode('utf-8', errors='replace')}
        result['log_capture_limit_reached'] = size >= config.COMMAND_JOB_LOG_BYTES
        result.update(texts)
        result['log_truncated'] = size > len(data)
        if len(encode(result)) > maximum:
            for name, value in texts.items():
                result[name] = ''
                result['log_truncated' if name == 'log_tail' else 'result_truncated'] |= bool(value)
            available = maximum - len(encode(result))
            nonempty = [name for name, value in texts.items() if value]
            for index, name in enumerate(nonempty):
                allowance = available // (len(nonempty) - index)
                value = texts[name]
                low, high = 0, len(value)
                while low < high:
                    middle = (low + high + 1) // 2
                    part = value[-middle:] if name == 'log_tail' else value[:middle]
                    if len(encode(part)) - 2 <= allowance:
                        low = middle
                    else:
                        high = middle - 1
                result[name] = (value[-low:] if low else '') if name == 'log_tail' else value[:low]
                available -= len(encode(result[name])) - 2
                result['log_truncated' if name == 'log_tail' else 'result_truncated'] = (
                    low < len(value) or (name == 'log_tail' and size > len(data)))
            # false 比 true 多一个字符；回收最终截断标记变化所需的空间。
            while len(encode(result)) > maximum:
                name = max(texts, key=lambda key: len(result[key]))
                result[name] = result[name][1:] if name == 'log_tail' else result[name][:-1]
                result['log_truncated' if name == 'log_tail' else 'result_truncated'] = True
        audit('command_job_inspected', job_id=identifier, status=result['status'], drained=drained,
              log_chars=len(result['log_tail']), log_truncated=result['log_truncated'],
              result_truncated=result['result_truncated'])
        return result

    def cancel(self, owner, identifier):
        with self.lock:
            record = self.status(owner, identifier)
            if identifier in self.events:
                self.events[identifier].set()
        audit('command_job_cancel_requested', job_id=identifier, owner=owner,
              runtime_profile=record.get('runtime_profile', 'system'),
              execution_environment=record.get('execution_environment', 'sandbox'))

    def stop(self):
        with self.lock:
            self.closed = True
            for event in self.events.values():
                event.set()

    def close(self):
        self.stop()
        self.executor.shutdown(wait=True)
