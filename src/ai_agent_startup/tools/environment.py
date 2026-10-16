"""环境诊断与显式项目依赖准备；所有运行仍在命令沙箱内。"""
from contextlib import nullcontext
import hashlib
import json
import math
from pathlib import Path
import shlex
import shutil
from typing import Literal

from pydantic import BaseModel, Field

from ai_agent_startup import config
from ai_agent_startup.tools.base import register_tool
from ai_agent_startup.tools.command import _execute
from ai_agent_startup.tools.sandbox import (
    SandboxError, ask_permission, audit, check_command, check_workspace_quota,
    command_memory_limit_mb, isolated_command, resolve_path, sandbox_root,
)


# 固定只读探测，不执行项目脚本；Node 必须初始化 V8 才算可用。
_PROBE_SCRIPT = r'''
import json, shutil, subprocess
probes = {
    'node': ['node', '-e', 'console.log(process.version + " execute=" + (1+2))'],
    'npm': ['npm', '--version'],
    'python': ['/usr/bin/python3', '-I', '-S', '-c', 'import sys; print(sys.version.split()[0])'],
    'uv': ['uv', '--version'],
    'java': ['java', '-version'],
    'maven': ['mvn', '-version'],
}
results = {}
for name, argv in probes.items():
    if shutil.which(argv[0]) is None:
        results[name] = {'status': 'missing', 'reason': 'not_visible_in_sandbox'}
        continue
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=3)
        text = (proc.stdout + proc.stderr)[:800]
        limited = any(word in text.lower() for word in (
            'out of memory', 'virtual memory', 'segmentedtable::initializetable'))
        results[name] = {'status': 'available' if proc.returncode == 0 else
                         'resource_limited' if limited else 'blocked',
                         'exit_code': proc.returncode, 'evidence': text}
    except subprocess.TimeoutExpired:
        results[name] = {'status': 'blocked', 'reason': 'probe_timeout'}
    except OSError as exc:
        results[name] = {'status': 'blocked', 'reason': type(exc).__name__}
print('AGENT_ENVIRONMENT_JSON=' + json.dumps(results))
'''


class InspectEnvironmentArgs(BaseModel):
    """在真实命令沙箱探测 Node/npm/Python/uv/Java/Maven，区分缺失、资源不足和宿主可见但未暴露。"""


@register_tool(InspectEnvironmentArgs, name='inspect_environment', timeout_s=30,
               concurrency='parallel', workspace_access=True)
def inspect_environment() -> str:
    profile = ('node' if config.COMMAND_NODE_MEMORY_MB and
               'node' in config.COMMAND_RUNTIME_PATHS else 'system')
    command = 'cd /tmp && /usr/bin/python3 -I -c ' + shlex.quote(_PROBE_SCRIPT)
    try:
        check_workspace_quota()
        argv = check_command(command)
        isolated = isolated_command(argv, runtime_profile=profile)
        output = _execute(command, '固定运行时探测（不运行项目代码）', argv, isolated, timeout=25)
        marker = 'AGENT_ENVIRONMENT_JSON='
        line = next((line for line in output.splitlines() if line.startswith(marker)), None)
        if line is None:
            raise SandboxError('环境探测未完成：' + output[:1000])
        runtimes = json.loads(line[len(marker):])
        audit('environment_inspected', statuses={name: row['status'] for name, row in runtimes.items()})
        candidates = {}
        for name in ('node', 'node.exe', 'uv'):
            path = shutil.which(name)
            if path:
                candidates[name] = path
        # NVM 通常没有进入启动服务的 PATH，只列候选，不在宿主执行它们。
        nvm = Path.home() / '.nvm/versions/node'
        if nvm.is_dir():
            candidates['nvm_node_prefixes'] = [str(path) for path in sorted(nvm.iterdir())[:8]
                                                if (path / 'bin/node').is_file()]
        return json.dumps({'runtimes': runtimes, 'runtime_profile': profile,
            'memory_limit_mb': command_memory_limit_mb(profile), 'host_candidates': candidates,
            'configured_runtimes': list(config.COMMAND_RUNTIME_PATHS),
            'prepare_enabled': config.ENABLE_ENVIRONMENT_PREPARE,
            'network': config.COMMAND_NETWORK,
            'note': 'host_candidates 仅为宿主候选，不证明沙箱可执行；安装依赖后仍须实际 build/test。'}, ensure_ascii=False)
    except (SandboxError, ValueError, OSError) as exc:
        audit('blocked', action='inspect_environment', reason=str(exc))
        return json.dumps({'runtimes': {name: {'status': 'blocked'} for name in
            ('node', 'npm', 'python', 'uv', 'java', 'maven')}, 'error': str(exc)}, ensure_ascii=False)


class PrepareProjectEnvironmentArgs(BaseModel):
    """依照现有 manifest/lockfile 在项目内安装 npm/uv 依赖，默认不运行安装脚本；不是测试通过声明。需要显式启用。"""
    manager: Literal['npm', 'uv'] = Field(description='npm 安装 Node 依赖；uv 安装 Python 依赖')
    project_dir: str = Field(default='.', description='工作区相对项目目录，不接受宿主绝对路径')
    network: bool = Field(default=False, description='仅在已配置 HTTPS 域名白名单时申请联网；离线默认')
    allow_scripts: bool = Field(default=False, description='仅 npm 可显式开启生命周期脚本；会显示在权限计划中')
    reason: str = Field(default='', description='说明项目准备目的及风险')


def _manifest(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise SandboxError(f'需要不含符号链接且不超过 4 MiB 的 manifest：{path.name}')
    with path.open('rb') as stream:
        content = stream.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024:
        raise SandboxError(f'manifest 超过 4 MiB：{path.name}')
    return hashlib.sha256(content).hexdigest()


def _check_install_paths(project: Path) -> None:
    for name in ('.agent-env', '.agent-env/python', '.agent-env/npm-cache',
                 '.agent-env/uv-cache', 'node_modules'):
        path = project / name
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            raise SandboxError('项目依赖目录含符号链接或越出项目，拒绝重定向安装：' + name)


@register_tool(PrepareProjectEnvironmentArgs, name='prepare_project_environment',
               timeout_s=config.ENVIRONMENT_PREPARE_TIMEOUT + config.CONFIRM_TIMEOUT + 10,
               workspace_access=True)
def prepare_project_environment(manager: str, project_dir: str = '.', network: bool = False,
                                allow_scripts: bool = False, reason: str = '') -> str:
    try:
        if not config.ENABLE_ENVIRONMENT_PREPARE:
            raise SandboxError('项目环境准备未启用（ENABLE_ENVIRONMENT_PREPARE=false）；不能据此宣称完整测试已通过')
        if manager not in {'npm', 'uv'}:
            raise SandboxError('仅支持 npm/uv；其他运行时须显式配置安全安装方式，不回退宿主安装')
        if '\\' in project_dir or '\x00' in project_dir or Path(project_dir).is_absolute() or '..' in Path(project_dir).parts or project_dir.startswith('@'):
            raise SandboxError('项目目录必须是工作区内的相对路径')
        project = resolve_path(project_dir, write=True)
        if not project.is_dir():
            raise SandboxError('项目目录不存在')
        _check_install_paths(project)
        check_workspace_quota()
        profile = 'node' if manager == 'npm' else 'system'
        # Zero means inherit; freeze one exact profile for approval and execution.
        limits = {
            'network_max_bytes': config.ENVIRONMENT_PREPARE_NETWORK_BYTES or config.COMMAND_NETWORK_MAX_BYTES,
            'network_max_connections': config.ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS or config.COMMAND_NETWORK_MAX_CONNECTIONS,
            'network_timeout_seconds': config.ENVIRONMENT_PREPARE_NETWORK_TIMEOUT or config.COMMAND_NETWORK_TIMEOUT,
            'file_limit_mb': config.ENVIRONMENT_PREPARE_FILE_MB or config.COMMAND_FILE_MB,
            'cpu_seconds': config.ENVIRONMENT_PREPARE_CPU_SECONDS or config.COMMAND_CPU_SECONDS,
        }
        files = ['package.json'] if manager == 'npm' else (
            ['pyproject.toml', 'uv.lock'] if (project / 'pyproject.toml').exists() else ['requirements.txt'])
        if manager == 'npm' and (project / 'package-lock.json').exists():
            files.append('package-lock.json')
        if manager == 'npm':
            for optional in ('npm-shrinkwrap.json', '.npmrc'):
                if (project / optional).exists() or (project / optional).is_symlink():
                    files.append(optional)
        fingerprints = {name: _manifest(project / name) for name in files}
        network_proxy = None
        if network:
            from ai_agent_startup.tools.network_proxy import NetworkProxy, validate_network_config
            validate_network_config()
            if config.COMMAND_NETWORK != 'allowlist':
                raise SandboxError('联网安装未启用；需要 COMMAND_NETWORK=allowlist 与精确域名白名单')
            # Validate bounds before asking permission; construction opens no socket.
            network_proxy = NetworkProxy(max_bytes=limits['network_max_bytes'],
                                         max_connections=limits['network_max_connections'],
                                         timeout=limits['network_timeout_seconds'])
        if manager == 'uv' and allow_scripts:
            raise SandboxError('uv 准备仅接受 wheel，不能启用源码构建脚本')
        relative = project.relative_to(sandbox_root()).as_posix()
        target = '/workspace' + ('/' + relative if relative != '.' else '')
        guard = 'import hashlib,json,pathlib; expected=json.loads(' + repr(json.dumps(fingerprints)) + '); ' + \
            'assert all(not pathlib.Path(p).is_symlink() and hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==v for p,v in expected.items()), "manifest changed after confirmation"'
        guard += '; root=pathlib.Path.cwd(); assert all(not (root/p).is_symlink() and (root/p).resolve().is_relative_to(root) for p in ' + repr(('.agent-env', '.agent-env/python', '.agent-env/npm-cache', '.agent-env/uv-cache', 'node_modules')) + '), "installation path changed after confirmation"'
        if manager == 'npm':
            install = 'npm ' + ('ci' if any(name in files for name in ('package-lock.json', 'npm-shrinkwrap.json')) else 'install')
            install += ' --no-audit --no-fund' + ('' if allow_scripts else ' --ignore-scripts')
            install += ' --global=false --workspaces=false --include-workspace-root=false --prefix=' + shlex.quote(target)
            install += ' --cache=' + shlex.quote(target + '/.agent-env/npm-cache')
            install += ' --logs-dir=' + shlex.quote(target + '/.agent-env/npm-cache/_logs')
            install += ' --userconfig=/dev/null --globalconfig=/tmp/agent-npm-global-config'
            fetch_timeout_ms = max(1, math.ceil(limits['network_timeout_seconds'] * 1000))
            install += f' --fetch-retries=0 --fetch-timeout={fetch_timeout_ms} --maxsockets=2'
            if not network:
                install += ' --offline'
            install += ' && node -e ' + shlex.quote('console.log("Node executes:", 1+2)')
        else:
            # 不重建用户 .venv；专用路径存在时，只复用本工具留下的标记环境。
            env_dir = project / '.agent-env/python'
            if env_dir.exists() and not (env_dir / '.agent-managed').is_file():
                raise SandboxError('受管 Python 环境路径已存在且不属于本工具，拒绝覆盖')
            env_target = target + '/.agent-env/python'
            install = 'export UV_PYTHON_DOWNLOADS=never UV_NO_CONFIG=1 UV_CACHE_DIR=' + shlex.quote(target + '/.agent-env/uv-cache')
            install += ' UV_PROJECT_ENVIRONMENT=' + shlex.quote(env_target) + '; '
            install += 'export UV_CONCURRENT_DOWNLOADS=1 UV_CONCURRENT_INSTALLS=1 UV_CONCURRENT_BUILDS=1; '
            if not network:
                install += 'export UV_OFFLINE=1; '
            install += 'if [ ! -f .agent-env/python/.agent-managed ]; then uv venv --no-config --python /usr/bin/python3 .agent-env/python && touch .agent-env/python/.agent-managed; fi && '
            if 'uv.lock' in files:
                install += 'uv sync --locked --no-config --no-build --no-install-project'
            else:
                install += 'uv pip install --no-config --only-binary=:all: --python .agent-env/python/bin/python -r requirements.txt'
            install += ' && .agent-env/python/bin/python -I -S -c ' + shlex.quote('import sys; print("Python executes:", sys.version.split()[0])')
        command = 'cd ' + shlex.quote(target) + ' && /usr/bin/python3 -I -c ' + shlex.quote(guard) + ' && ( ' + install + ' )'
        argv = check_command(command)
        isolated = isolated_command(argv, runtime_profile=profile,
                                    file_limit_mb=limits['file_limit_mb'], cpu_seconds=limits['cpu_seconds'])
        plan = json.dumps({'manager': manager, 'project_dir': project_dir,
            'manifest_sha256': fingerprints, 'allow_scripts': allow_scripts,
            'network': network, 'domains': config.COMMAND_NETWORK_ALLOWLIST if network else [],
            'timeout_seconds': config.ENVIRONMENT_PREPARE_TIMEOUT,
            'runtime_profile': profile, 'memory_limit_mb': command_memory_limit_mb(profile),
            **limits, 'workspace_limit_mb': config.WORKSPACE_LIMIT_MB,
            'command': command}, ensure_ascii=False)
        if not ask_permission('prepare_project_environment', plan, reason, command=command, force_confirmation=True):
            return '[已取消] 用户未确认，未安装依赖；真实构建/测试尚未完成。'
        with network_proxy if network else nullcontext() as proxy:
            if proxy is not None:
                isolated = isolated_command(argv, network_socket=proxy.path, runtime_profile=profile,
                                            file_limit_mb=limits['file_limit_mb'], cpu_seconds=limits['cpu_seconds'])
            audit('environment_prepare_started', manager=manager, project_dir=project_dir,
                  manifest_sha256=fingerprints, network=network, allow_scripts=allow_scripts,
                  timeout_seconds=config.ENVIRONMENT_PREPARE_TIMEOUT, **limits)
            result = _execute(command, reason, argv, isolated, timeout=config.ENVIRONMENT_PREPARE_TIMEOUT,
                              file_limit_mb=limits['file_limit_mb'])
            if proxy is not None and proxy.denial_summary():
                result += '\n[网络边界诊断] ' + proxy.denial_summary() + '；这是本地网关拒绝，不代表包仓库或依赖不存在。'
        audit('environment_prepare_finished', manager=manager, project_dir=project_dir,
              success=result.startswith('[退出码] 0\n'))
        return result + '\n[验证范围] 本操作仅准备依赖并验证运行时能执行，未执行项目 build/test。继续调用 run_command 运行真实构建和测试；失败时可能保留部分依赖，不自动删除用户文件。'
    except (SandboxError, ValueError, OSError) as exc:
        audit('blocked', action='prepare_project_environment', reason=str(exc))
        return f'[已拦截] {exc}；未完成真实构建/测试。'
