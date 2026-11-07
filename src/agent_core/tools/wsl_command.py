"""Windows 原生运行时与 WSL 隔离工具之间的有界协议。"""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from agent_core import config
from agent_core.core.files import directory_identity, opened_path
from agent_core.tools.sandbox import SandboxError, _context, audit, confirm, current_policy, sandbox_root


CONFIG_KEYS = '''COMMAND_CPU_SECONDS COMMAND_FILE_MB COMMAND_NETWORK
COMMAND_NETWORK_ALLOWLIST COMMAND_NETWORK_MAX_BYTES COMMAND_NETWORK_MAX_CONNECTIONS
COMMAND_NETWORK_TIMEOUT COMMAND_PROCESSES COMMAND_QUOTA_INTERVAL COMMAND_TIMEOUT
COMMAND_MEMORY_MB COMMAND_NODE_MEMORY_MB COMMAND_RUNTIME_PATHS CONFIRM_TIMEOUT
ENVIRONMENT_PREPARE_CPU_SECONDS ENVIRONMENT_PREPARE_FILE_MB
ENVIRONMENT_PREPARE_NETWORK_BYTES ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS
ENVIRONMENT_PREPARE_NETWORK_TIMEOUT ENVIRONMENT_PREPARE_TIMEOUT
ENABLE_ENVIRONMENT_PREPARE LOCAL_COMMAND_TIMEOUT TOOL_MAX_OUTPUT TOOL_PERMISSION_RULES
TOOL_ROOTS WORKSPACE_LIMIT_MB'''.split()
MAX_LINE = 1024 * 1024


def _worker_script() -> Path:
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', None)
        if base is None:
            raise SandboxError('打包的 WSL 执行器不可用')
        script = Path(base) / 'wsl_runtime' / 'agent_core' / 'tools' / 'wsl_command_worker.py'
        if not script.is_file():
            raise SandboxError('打包的 WSL 执行器不可用')
        return script
    return Path(__file__).with_name('wsl_command_worker.py')


def _launcher() -> list[str]:
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'wsl.exe'
    if not executable.is_file():
        raise SandboxError('隔离命令需要 WSL2，请安装并配置 WINDOWS_WSL_DISTRIBUTION')
    return [str(executable), '--distribution', config.WINDOWS_WSL_DISTRIBUTION, '--exec']


def _environment() -> dict[str, str]:
    # 不传递模型密钥、WSLENV 和任意用户环境。
    return {key: value for key, value in os.environ.items()
            if key in {'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA'}}


def _translate(path: str | Path, *, reverse=False) -> str:
    result = subprocess.run([*_launcher(), '/usr/bin/wslpath', '-a', '-w' if reverse else '-u', str(path)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                            encoding='utf-8', errors='replace', env=_environment(),
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    value = result.stdout.strip()
    if result.returncode or not value or any(ord(char) < 32 for char in value):
        raise SandboxError('无法映射 WSL 工作区，请检查发行版与磁盘挂载')
    return value


def _settings() -> dict:
    values = {key: getattr(config, key) for key in CONFIG_KEYS}
    # Linux 运行时绑定采用显式 Linux 路径，Windows 路径则先翻译。
    values['COMMAND_RUNTIME_PATHS'] = {
        key: _translate(Path(value)) if Path(value).drive else value
        for key, value in values['COMMAND_RUNTIME_PATHS'].items()}
    values['TOOL_ROOTS'] = {name: {**entry, 'path': _translate(Path(entry['path']))}
                           for name, entry in values['TOOL_ROOTS'].items()}
    return values


def execute_wsl_tool(operation: str, arguments: dict) -> str:
    """保持原生目录授权直到 Linux 工具完成取消、进程回收和审计。"""
    if os.name != 'nt' or operation not in {'run_command', 'prepare_project_environment', 'inspect_environment'}:
        raise SandboxError('不支持的 WSL 工具调用')
    root = sandbox_root()
    context = _context.get()
    identity = directory_identity(root)
    with opened_path(root, root, directory=True, expected_identity=identity):
        linux_root = _translate(root)
        if Path(_translate(linux_root, reverse=True)) != root or directory_identity(root) != identity:
            raise SandboxError('WSL 映射与已选择工作区不匹配')
        script = _translate(_worker_script())
        identifier = uuid.uuid4().hex
        request = {'id': identifier, 'operation': operation, 'arguments': arguments,
                   'root': linux_root, 'config': _settings(), 'policy': current_policy(),
                   'paths': {key: _translate(Path(getattr(config, key)))
                             for key in ('PROJECT_ROOT', 'AGENT_STATE_DIR', 'AUDIT_LOG')},
                   'execution_environment': 'sandbox',
                   'flow_workspace_scope': bool(context and context.flow_workspace_scope)}
        argv = [*_launcher(), '/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin',
                'LANG=C.UTF-8', 'PYTHONIOENCODING=utf-8', 'PYTHON_DOTENV_DISABLED=1',
                'AI_AGENT_PROJECT_ROOT=/tmp/channel-agent-wsl-worker',
                config.WINDOWS_WSL_PYTHON, '-B', '-u', script]
        audit('wsl_tool_started', operation=operation, request_id=identifier)
        return _exchange(argv, request, context.cancelled if context else None)


def _exchange(argv: list[str], request: dict, cancelled) -> str:
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=_environment(), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    events = queue.Queue(maxsize=64)
    errors = bytearray()
    send_lock = threading.Lock()

    def send(record):
        data = json.dumps({'id': request['id'], **record}, ensure_ascii=False).encode('utf-8') + b'\n'
        if len(data) > MAX_LINE:
            raise SandboxError('WSL 请求超过协议上限')
        with send_lock:
            process.stdin.write(data)
            process.stdin.flush()

    def read_output():
        try:
            while True:
                line = process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE or not line.endswith(b'\n'):
                    events.put(('invalid', None))
                    # 无效数据仍需排空，避免阻塞执行器的退出。
                    while process.stdout.read(65536):
                        pass
                    return
                events.put(('record', line))
        finally:
            events.put(('eof', None))

    def read_errors():
        while data := process.stderr.read(4096):
            if len(errors) < 8192:
                errors.extend(data[:8192 - len(errors)])

    readers = [threading.Thread(target=read_output, daemon=True), threading.Thread(target=read_errors, daemon=True)]
    for reader in readers:
        reader.start()
    result = None
    failure = ''
    sent_cancel = False
    ready = False
    maximum = max(config.COMMAND_TIMEOUT, config.ENVIRONMENT_PREPARE_TIMEOUT) + 6 * config.CONFIRM_TIMEOUT + 60
    deadline = time.monotonic() + maximum
    try:
        send(request)
        while True:
            if not sent_cancel and (cancelled is not None and cancelled.is_set() or time.monotonic() >= deadline):
                try:
                    send({'type': 'cancel'})
                except OSError:
                    pass
                sent_cancel = True
            try:
                kind, data = events.get(timeout=.1)
            except queue.Empty:
                continue
            if kind == 'eof':
                break
            try:
                if kind == 'invalid':
                    raise ValueError('无效协议长度')
                record = json.loads(data)
                if not isinstance(record, dict) or record.get('id') != request['id'] or result is not None:
                    raise ValueError('无效协议记录')
                event = record.get('type')
                if event == 'ready' and not ready:
                    ready = True
                elif event == 'confirm' and ready:
                    allowed = not sent_cancel and confirm(record['prompt'], float(record['timeout']))
                    send({'type': 'decision', 'request_id': record['request_id'], 'allowed': bool(allowed)})
                elif event == 'audit' and ready and isinstance(record.get('entry'), dict):
                    entry = record['entry']
                    audit('wsl_tool_audit', strict=True, request_id=request['id'], worker_event=entry)
                    send({'type': 'audit_ack', 'event_id': record['event_id']})
                elif event == 'result' and ready and isinstance(record.get('result'), str):
                    result = record['result']
                elif event == 'error':
                    failure = record.get('error', 'WSL 执行失败')
                else:
                    raise ValueError('无效协议事件')
            except (ValueError, KeyError, TypeError, OSError):
                failure = 'WSL 审计或协议处理失败'
                if not sent_cancel:
                    try:
                        send({'type': 'cancel'})
                    except OSError:
                        pass
                    sent_cancel = True
        process.wait()
        if failure or process.returncode or result is None:
            audit('wsl_tool_failed', request_id=request['id'], returncode=process.returncode,
                  diagnostic=errors.decode('utf-8', errors='replace'))
            raise SandboxError(failure or 'WSL 执行器未能启动或完成，请检查 WINDOWS_WSL_PYTHON 的 Linux 依赖')
        audit('wsl_tool_finished', request_id=request['id'], cancelled=sent_cancel)
        return result
    finally:
        # EOF 会请求 Linux 工具取消。等待 worker 真正结束才释放原生目录句柄。
        try:
            process.stdin.close()
        except OSError:
            pass
        process.wait()
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()
