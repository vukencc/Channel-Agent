"""显式 Linux 本机执行的最小进程环境与尽力子进程回收。"""
import os
from pathlib import Path
import re
import signal
import sys
import shutil
import stat

from agent_core import config
from agent_core.tools.sandbox import SandboxError, cancellation_requested, sandbox_root


def check_command_workspace(root: Path) -> None:
    """拒绝项目内嵌套挂载和连接到项目外的现存硬链接。"""
    try:
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields = line.split()
            if len(fields) < 6:
                raise ValueError('mountinfo 格式无效')
            mount = Path(re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), fields[4]))
            if mount != root and mount.is_relative_to(root):
                raise SandboxError('工作区内含嵌套挂载，拒绝执行隔离命令')
        links: dict[tuple[int, int], tuple[int, int]] = {}
        def fail(error):
            raise error
        for directory, names, files in os.walk(root, followlinks=False, onerror=fail):
            if cancellation_requested():
                raise SandboxError('已取消，命令未启动')
            for name in (*names, *files):
                info = os.lstat(Path(directory) / name)
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    count, expected = links.get(key, (0, info.st_nlink))
                    if expected != info.st_nlink:
                        raise SandboxError('工作区硬链接在检查期间改变')
                    links[key] = (count + 1, expected)
        if any(count != expected for count, expected in links.values()):
            raise SandboxError('工作区含指向外部的硬链接，拒绝执行隔离命令')
    except (OSError, ValueError) as exc:
        raise SandboxError('无法检查工作区的挂载或硬链接，拒绝执行') from exc


def flow_local_command(argv: list[str], environment: dict[str, str], protected_paths=()) -> tuple[list[str], dict[str, str]]:
    """保留宿主网络和运行时，只允许选定项目持久写入。"""
    executable = shutil.which('bwrap', path='/usr/bin:/bin')
    if sys.platform != 'linux' or not executable:
        raise SandboxError('Flow 本机命令需要 Linux bubblewrap；拒绝无隔离执行')
    root = sandbox_root()
    check_command_workspace(root)
    protected = [Path(p).resolve() for p in protected_paths]
    protected += [Path(config.PROJECT_ROOT).resolve(), Path(config.AGENT_STATE_DIR).resolve(),
                  Path(config.AUDIT_LOG).resolve()]
    masks = [Path('/home'), Path('/root'), Path('/tmp'), Path('/run')]
    for path in protected:
        if path == Path('/') or path == root:
            raise SandboxError('Flow 保护路径与项目工作区冲突')
        if not any(path == base or path.is_relative_to(base) for base in masks):
            if root.is_relative_to(path):
                # 默认工作区可能位于状态目录内，随后只重新绑定该工作区。
                pass
            elif path.is_relative_to(root):
                raise SandboxError('Flow 保护路径与项目工作区冲突')
            masks.append(path)
    args = [executable, '--unshare-user', '--unshare-pid', '--die-with-parent',
            '--new-session', '--cap-drop', 'ALL', '--ro-bind', '/', '/']
    for path in sorted(set(masks), key=lambda p: len(p.parts)):
        if any(path != other and path.is_relative_to(other) for other in masks):
            continue
        if path.is_dir():
            args += ['--tmpfs', str(path)]
        elif path.exists():
            args += ['--ro-bind', '/dev/null', str(path)]
    args += ['--proc', '/proc', '--dev', '/dev']

    def parents(path: Path) -> None:
        for parent in reversed(path.parents):
            if parent != Path('/'):
                args.extend(['--dir', str(parent)])

    # 仅暴露受信任的 NVM 版本目录及 uv 本体，避免暴露整个用户目录。
    home = Path.home().resolve()
    runtimes = []
    for item in environment.get('PATH', '').split(':'):
        path = Path(item)
        if path.name == 'bin' and path.parent.parent.name == 'node' and path.parent.parent.parent.name == 'versions' and path.is_relative_to(home / '.nvm'):
            runtimes.append((path.parent, path.parent))
    uv = shutil.which('uv', path=environment.get('PATH', ''))
    if uv:
        runtimes.append((Path(uv).resolve(), Path(uv)))
    for path, target in runtimes:
        if path.exists() and not any(path == p or path.is_relative_to(p) or p.is_relative_to(path) for p in protected):
            parents(target)
            args += ['--ro-bind', str(path), str(target)]
    parents(root)
    args += ['--bind', str(root), str(root), '--chdir', str(root), '--', *argv]
    environment = dict(environment)
    environment.update(HOME='/tmp', TMPDIR='/tmp', TMP='/tmp', TEMP='/tmp',
                       XDG_CACHE_HOME='/tmp/.cache', GRADLE_USER_HOME='/tmp/.gradle',
                       UV_LINK_MODE='copy')
    return args, environment


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
