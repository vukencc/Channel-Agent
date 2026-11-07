"""WSL 内部命令 worker：仅接受可信原生进程的有界 JSONL 协议。"""
import json
import importlib.util
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ['PYTHON_DOTENV_DISABLED'] = '1'

MAX_LINE = 1024 * 1024
CONFIG_KEYS = frozenset('''COMMAND_CPU_SECONDS COMMAND_FILE_MB COMMAND_NETWORK
COMMAND_NETWORK_ALLOWLIST COMMAND_NETWORK_MAX_BYTES COMMAND_NETWORK_MAX_CONNECTIONS
COMMAND_NETWORK_TIMEOUT COMMAND_PROCESSES COMMAND_QUOTA_INTERVAL COMMAND_TIMEOUT
COMMAND_MEMORY_MB COMMAND_NODE_MEMORY_MB COMMAND_RUNTIME_PATHS CONFIRM_TIMEOUT
ENVIRONMENT_PREPARE_CPU_SECONDS ENVIRONMENT_PREPARE_FILE_MB
ENVIRONMENT_PREPARE_NETWORK_BYTES ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS
ENVIRONMENT_PREPARE_NETWORK_TIMEOUT ENVIRONMENT_PREPARE_TIMEOUT
ENABLE_ENVIRONMENT_PREPARE LOCAL_COMMAND_TIMEOUT TOOL_MAX_OUTPUT TOOL_PERMISSION_RULES
TOOL_ROOTS WORKSPACE_LIMIT_MB'''.split())


_input_buffer = bytearray()


def _read_line():
    while b'\n' not in _input_buffer:
        if len(_input_buffer) > MAX_LINE:
            raise ValueError('输入过大')
        chunk = os.read(sys.stdin.fileno(), min(4096, MAX_LINE + 1 - len(_input_buffer)))
        if not chunk:
            raise ValueError('输入为空或未结束')
        _input_buffer.extend(chunk)
    end = _input_buffer.index(b'\n')
    if end + 1 > MAX_LINE:
        raise ValueError('输入过大')
    line = bytes(_input_buffer[:end])
    del _input_buffer[:end + 1]
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError('输入必须是 JSON 对象')
    return value


def main():
    request_id = None
    output_lock = threading.Lock()

    def emit(kind, **fields):
        record = {'type': kind, 'id': request_id, **fields}
        line = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
        if len(line.encode('utf-8')) > MAX_LINE:
            raise ValueError('输出超过协议上限')
        with output_lock:
            sys.stdout.write(line + '\n')
            sys.stdout.flush()

    try:
        request = _read_line()
        request_id = request.get('id')
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError('无效请求标识')
        if request.get('operation') not in {'run_command', 'prepare_project_environment', 'inspect_environment'}:
            raise ValueError('不支持的工具')
        if request.get('execution_environment') != 'sandbox':
            raise ValueError('仅支持 sandbox')
        if request.get('policy') not in {'readonly', 'smart', 'standard', 'trusted', 'full_access'}:
            raise ValueError('无效权限策略')
        settings = request.get('config')
        arguments = request.get('arguments')
        if not isinstance(settings, dict) or not settings.keys() <= CONFIG_KEYS or not isinstance(arguments, dict):
            raise ValueError('无效配置或参数')
        root = Path(request['root'])
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise ValueError('无效工作区')
        stat = root.stat()
        # 只绑定 Core 的纯 Python 包，不能让 Windows site-packages 遮蔽 Linux 扩展。
        package = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('agent_core', package / '__init__.py',
                                                       submodule_search_locations=[str(package)])
        if spec is None or spec.loader is None:
            raise ValueError('Core 包不可用')
        core = importlib.util.module_from_spec(spec)
        sys.modules['agent_core'] = core
        spec.loader.exec_module(core)
        from agent_core import config
        from agent_core.tools.base import TOOL_REGISTRY
        from agent_core.tools import command, environment  # noqa: F401
        from agent_core.tools.execution_environment import _check_flow_workspace
        from agent_core.tools.sandbox import ToolContext, tool_context
        paths = request.get('paths', {})
        if not isinstance(paths, dict) or not paths.keys() <= {'PROJECT_ROOT', 'AGENT_STATE_DIR', 'AUDIT_LOG'}:
            raise ValueError('无效保护路径')
        for name, value in paths.items():
            if not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts:
                raise ValueError('保护路径必须为 Linux 绝对路径')
            setattr(config, name, Path(value))
        for key, value in settings.items():
            default = getattr(config, key)
            numeric = type(value) in (int, float) and type(default) in (int, float)
            if numeric:
                if not math.isfinite(value):
                    raise ValueError('配置必须为有限数值：' + key)
            elif type(value) is not type(default):
                raise ValueError('配置类型无效：' + key)
            setattr(config, key, value)
        if request.get('flow_workspace_scope', False):
            _check_flow_workspace(root)
        cancelled = threading.Event()
        deadline = threading.Timer(max(config.COMMAND_TIMEOUT, config.ENVIRONMENT_PREPARE_TIMEOUT)
                                   + 5 * config.CONFIRM_TIMEOUT + 30, cancelled.set)
        deadline.daemon = True
        deadline.start()
        condition = threading.Condition()
        decisions = {}
        pending = set()
        pending_audits = set()
        audit_acks = set()
        input_closed = threading.Event()

        def control():
            try:
                while True:
                    message = _read_line()
                    if message.get('id') != request_id:
                        raise ValueError('请求标识不匹配')
                    if message.get('type') == 'cancel':
                        cancelled.set()
                        with condition:
                            condition.notify_all()
                    elif message.get('type') == 'decision':
                        key = message.get('request_id')
                        allowed = message.get('allowed')
                        if not isinstance(key, str) or type(allowed) is not bool:
                            raise ValueError('无效确认')
                        with condition:
                            if key not in pending or key in decisions:
                                raise ValueError('确认标识不匹配')
                            decisions[key] = allowed
                            condition.notify_all()
                    elif message.get('type') == 'audit_ack':
                        key = message.get('event_id')
                        with condition:
                            if key not in pending_audits or key in audit_acks:
                                raise ValueError('审计标识不匹配')
                            audit_acks.add(key)
                            condition.notify_all()
                    else:
                        raise ValueError('无效控制消息')
            except (ValueError, OSError, json.JSONDecodeError):
                cancelled.set()
                input_closed.set()
                with condition:
                    condition.notify_all()

        threading.Thread(target=control, daemon=True).start()
        counter = 0

        def confirm(prompt, timeout):
            nonlocal counter
            counter += 1
            key = str(counter)
            with condition:
                pending.add(key)
            emit('confirm', request_id=key, prompt=prompt, timeout=timeout)
            deadline = time.monotonic() + timeout
            with condition:
                while key not in decisions and not cancelled.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        pending.discard(key)
                        return False
                    condition.wait(remaining)
                pending.discard(key)
                allowed = decisions.pop(key, False) and not cancelled.is_set()
            if allowed and request.get('flow_workspace_scope', False):
                _check_flow_workspace(root)
            return allowed

        with tempfile.TemporaryDirectory(prefix='agent-wsl-audit-') as directory:
            audit_path = Path(directory) / 'audit.jsonl'
            from agent_core.tools import sandbox
            append_audit = sandbox.append_audit
            audit_counter = 0

            def persist_audit(path, line, sync=False):
                nonlocal audit_counter
                try:
                    append_audit(path, line, sync=sync)
                except OSError as exc:
                    cancelled.set()
                    raise RuntimeError('WSL 审计写入失败') from exc
                audit_counter += 1
                key = str(audit_counter)
                with condition:
                    pending_audits.add(key)
                try:
                    emit('audit', event_id=key, entry=json.loads(line))
                except OSError as exc:
                    cancelled.set()
                    raise RuntimeError('WSL 审计传输失败') from exc
                end = time.monotonic() + 15
                with condition:
                    while key not in audit_acks and not input_closed.is_set():
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            cancelled.set()
                            raise RuntimeError('原生审计确认超时')
                        condition.wait(remaining)
                    pending_audits.discard(key)
                    audit_acks.discard(key)
                if input_closed.is_set():
                    raise RuntimeError('原生审计连接已关闭')

            sandbox.append_audit = persist_audit
            context = ToolContext(root, audit_path, confirm, cancelled,
                permission_policy=request['policy'], root_identity=(stat.st_dev, stat.st_ino),
                execution_environment='sandbox', flow_workspace_scope=bool(request.get('flow_workspace_scope', False)))
            emit('ready', identity=[stat.st_dev, stat.st_ino])
            with tool_context(context):
                result = TOOL_REGISTRY[request['operation']].run(json.dumps(arguments))
            emit('result', result=result)
        return 0
    except Exception as exc:
        try:
            emit('error', error=f'{type(exc).__name__}: {exc}')
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
