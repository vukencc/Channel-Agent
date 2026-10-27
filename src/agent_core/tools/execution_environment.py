"""显式 Linux 本机执行的最小进程环境与尽力子进程回收。"""
import os
from pathlib import Path
import re
import signal
import sys

from agent_core import config
from agent_core.tools.sandbox import SandboxError, sandbox_root


def validate_local_execution() -> Path:
    """本机模式只允许 Linux，且仍验证用户选择的工作区身份。"""
    if sys.platform != 'linux':
        raise SandboxError('本机命令执行仅支持 Linux/WSL；请在 Linux 服务中显式选择 local')
    return sandbox_root()


def local_process_environment() -> dict[str, str]:
    """只传运行时所需变量；不将应用或模型密钥传给子进程。"""
    allowed = {'HOME', 'LANG', 'LANGUAGE', 'TZ', 'TMPDIR', 'TMP', 'TEMP',
               'USER', 'LOGNAME', 'SHELL', 'JAVA_HOME', 'JDK_HOME',
               'M2_HOME', 'MAVEN_HOME', 'GRADLE_USER_HOME',
               # 网络依赖系统代理时必须保留配置；仍不继承应用/模型密钥。
               'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
               'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}
    environment = {name: value for name, value in os.environ.items()
                   if name in allowed or re.fullmatch(r'LC_[A-Z_]+', name)}
    paths = []
    for value in os.environ.get('PATH', '').split(':'):
        # WSL 注入的 Windows 程序不可作为 Linux 运行时；也不接受相对 PATH。
        if (value.startswith('/') and '\\' not in value
                and not re.match(r'^/mnt/[a-z](?:/|$)', value, re.IGNORECASE)
                and value not in paths):
            paths.append(value)
    runtimes = config.COMMAND_RUNTIME_PATHS
    additions = []
    if runtimes.get('node'):
        additions.append(str(Path(runtimes['node']) / 'bin'))
    if runtimes.get('uv'):
        additions.append(str(Path(runtimes['uv']).parent))
    additions.append(str(Path.home() / '.local/bin'))
    for value in [*additions, '/usr/local/bin', '/usr/bin', '/bin']:
        if (Path(value).is_absolute() and '\\' not in value
                and not re.match(r'^/mnt/[a-z](?:/|$)', value, re.IGNORECASE)
                and value not in paths):
            paths.append(value)
    environment['PATH'] = ':'.join(paths)
    # 开发服务的环回连接无需外部代理；保留用户已有的其它绕过域名。
    bypass = []
    for name in ('no_proxy', 'NO_PROXY'):
        for value in environment.get(name, '').split(','):
            value = value.strip()
            if value and value not in bypass:
                bypass.append(value)
    for value in ('localhost', '127.0.0.1', '::1'):
        if value not in bypass:
            bypass.append(value)
    environment['no_proxy'] = environment['NO_PROXY'] = ','.join(bypass)
    environment.setdefault('HOME', str(Path.home()))
    environment.setdefault('LANG', 'C.UTF-8')
    return environment


class LocalProcessTree:
    """按 PID 与启动时间跟踪普通后代，包含已调用 setsid 的子进程。

    这是单用户本机进程的清理手段，不提供恶意守护进程隔离保证。
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.processes: dict[int, str] = {}
        self.update()

    @staticmethod
    def _snapshot() -> dict[int, tuple[int, str]]:
        records = {}
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                # comm 字段可以包含空格与括号，使用最后一个右括号定位字段。
                fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                records[int(path.name)] = (int(fields[1]), fields[19])
            except (OSError, ValueError, IndexError):
                continue
        return records

    def update(self) -> None:
        records = self._snapshot()
        if self.pid in records and self.pid not in self.processes:
            self.processes[self.pid] = records[self.pid][1]
        known = {pid for pid, started in self.processes.items()
                 if pid in records and records[pid][1] == started}
        while True:
            children = {pid for pid, (parent, _) in records.items()
                        if parent in known and pid not in known}
            if not children:
                break
            known.update(children)
            for pid in children:
                self.processes[pid] = records[pid][1]

    def terminate(self) -> None:
        self.update()
        current = self._snapshot()
        # 先后代再父进程；启动时间匹配避免误杀复用 PID。
        for pid, started in reversed(list(self.processes.items())):
            if pid in current and current[pid][1] == started:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
