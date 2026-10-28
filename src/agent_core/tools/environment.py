"""在用户选定的沙箱或 Linux 本机环境中诊断与准备项目依赖。"""
from contextlib import nullcontext
import hashlib
import json
import math
from pathlib import Path
import shlex
import shutil
from typing import Literal

from pydantic import BaseModel, Field

from agent_core import config
from agent_core.tools.base import register_tool
from agent_core.tools.command import _execute, development_resource_limits
from agent_core.tools.sandbox import (
    SandboxError, ask_permission, audit, check_command, check_workspace_quota,
    command_memory_limit_mb, isolated_command, resolve_path, sandbox_root,
    execution_environment,
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
    """在会话选定的 sandbox/local 环境实际探测 Node/npm/Python/uv/Java/Maven，区分缺失与执行失败。"""


@register_tool(InspectEnvironmentArgs, name='inspect_environment', timeout_s=30,
               concurrency='parallel', workspace_access=True)
def inspect_environment() -> str:
    mode = execution_environment()
    local = mode == 'local'
    profile = ('node' if config.COMMAND_NODE_MEMORY_MB and
               'node' in config.COMMAND_RUNTIME_PATHS else 'system')
    probe_script = (_PROBE_SCRIPT.replace('not_visible_in_sandbox', 'not_visible_in_local_path')
                    if local else _PROBE_SCRIPT)
    command = ('' if local else 'cd /tmp && ') + '/usr/bin/python3 -I -c ' + shlex.quote(probe_script)
    try:
        options = {}
        if local:
            from agent_core.tools.execution_environment import local_process_environment, validate_local_execution
            validate_local_execution()
            options = {'environment': local_process_environment(), 'check_quota': False,
                       'execution_mode': 'local'}
        else:
            check_workspace_quota()
        argv = check_command(command)
        isolated = argv if local else isolated_command(argv, runtime_profile=profile)
        output = _execute(command, '固定运行时探测（不运行项目代码）', argv, isolated,
                          timeout=min(25, config.LOCAL_COMMAND_TIMEOUT) if local else 25, **options)
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
            'execution_environment': mode,
            'working_directory': str(sandbox_root()) if local else '/workspace',
            'memory_limit_mb': None if local else command_memory_limit_mb(profile), 'host_candidates': candidates,
            'configured_runtimes': list(config.COMMAND_RUNTIME_PATHS),
            'prepare_enabled': local or config.ENABLE_ENVIRONMENT_PREPARE,
            'network': 'host_including_localhost' if local else config.COMMAND_NETWORK,
            'note': ('已实际探测 Linux 本机运行时；安装依赖后仍须实际 build/test。' if local else
                     'host_candidates 仅为宿主候选，不证明沙箱可执行；安装依赖后仍须实际 build/test。')}, ensure_ascii=False)
    except (SandboxError, ValueError, OSError) as exc:
        audit('blocked', action='inspect_environment', reason=str(exc))
        return json.dumps({'runtimes': {name: {'status': 'blocked'} for name in
            ('node', 'npm', 'python', 'uv', 'java', 'maven')}, 'error': str(exc)}, ensure_ascii=False)


class PrepareProjectEnvironmentArgs(BaseModel):
    """依照项目 manifest/lockfile 安装 npm/uv 依赖，默认不运行安装脚本；不是测试通过声明。sandbox 需启用准备开关，显式 local 使用本机网络及资源。"""
    manager: Literal['npm', 'uv'] = Field(description='npm 安装 Node 依赖；uv 安装 Python 依赖')
    project_dir: str = Field(default='.', description='工作区相对项目目录，不接受宿主绝对路径')
    network: bool = Field(default=False, description='仅 sandbox：申请已配置白名单网络，默认离线；local 使用本机网络含 localhost')
    allow_scripts: bool = Field(default=False, description='显式允许 npm 生命周期脚本或 uv 源码构建/项目安装脚本；默认禁止，会显示在权限计划中')
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
               timeout_s=max(config.ENVIRONMENT_PREPARE_TIMEOUT, config.LOCAL_COMMAND_TIMEOUT) + config.CONFIRM_TIMEOUT + 10,
               workspace_access=True)
def prepare_project_environment(manager: str, project_dir: str = '.', network: bool = False,
                                allow_scripts: bool = False, reason: str = '') -> str:
    try:
        mode = execution_environment()
        local = mode == 'local'
        if local:
            from agent_core.tools.execution_environment import local_process_environment, validate_local_execution
            validate_local_execution()
        if not local and not config.ENABLE_ENVIRONMENT_PREPARE:
            raise SandboxError('项目环境准备未启用（ENABLE_ENVIRONMENT_PREPARE=false）；不能据此宣称完整测试已通过')
        if manager not in {'npm', 'uv'}:
            raise SandboxError('仅支持 npm/uv；其他运行时须显式配置安全安装方式，不回退宿主安装')
        if '\\' in project_dir or '\x00' in project_dir or Path(project_dir).is_absolute() or '..' in Path(project_dir).parts or project_dir.startswith('@'):
            raise SandboxError('项目目录必须是工作区内的相对路径')
        project = resolve_path(project_dir, write=True)
        if not project.is_dir():
            raise SandboxError('项目目录不存在')
        _check_install_paths(project)
        if not local:
            check_workspace_quota()
        profile = 'node' if manager == 'npm' else 'system'
        limits = development_resource_limits()
        timeout = config.LOCAL_COMMAND_TIMEOUT if local else config.ENVIRONMENT_PREPARE_TIMEOUT
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
        if network and not local:
            from agent_core.tools.network_proxy import NetworkProxy, validate_network_config
            validate_network_config()
            if config.COMMAND_NETWORK != 'allowlist':
                raise SandboxError('联网安装未启用；需要 COMMAND_NETWORK=allowlist 与精确域名白名单')
            # Validate bounds before asking permission; construction opens no socket.
            network_proxy = NetworkProxy(max_bytes=limits['network_max_bytes'],
                                         max_connections=limits['network_max_connections'],
                                         timeout=limits['network_timeout_seconds'])
        relative = project.relative_to(sandbox_root()).as_posix()
        target = str(project) if local else '/workspace' + ('/' + relative if relative != '.' else '')
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
            fetch_timeout_ms = max(1, math.ceil((timeout if local else limits['network_timeout_seconds']) * 1000))
            install += f' --fetch-retries=0 --fetch-timeout={fetch_timeout_ms} --maxsockets=2'
            if not network and not local:
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
            if not network and not local:
                install += 'export UV_OFFLINE=1; '
            install += 'if [ ! -f .agent-env/python/.agent-managed ]; then uv venv --no-config --python /usr/bin/python3 .agent-env/python && touch .agent-env/python/.agent-managed; fi && '
            if 'uv.lock' in files:
                install += 'uv sync --locked --no-config' + ('' if allow_scripts else ' --no-build --no-install-project')
            else:
                install += 'uv pip install --no-config' + ('' if allow_scripts else ' --only-binary=:all:')
                install += ' --python .agent-env/python/bin/python -r requirements.txt'
            install += ' && .agent-env/python/bin/python -I -S -c ' + shlex.quote('import sys; print("Python executes:", sys.version.split()[0])')
        command = 'cd ' + shlex.quote(target) + ' && /usr/bin/python3 -I -c ' + shlex.quote(guard) + ' && ( ' + install + ' )'
        argv = check_command(command)
        isolated = argv if local else isolated_command(argv, runtime_profile=profile,
                                    file_limit_mb=limits['file_limit_mb'], cpu_seconds=limits['cpu_seconds'])
        plan = json.dumps({'manager': manager, 'project_dir': project_dir,
            'manifest_sha256': fingerprints, 'allow_scripts': allow_scripts,
            'script_policy': (('允许源码构建和项目安装脚本' if manager == 'uv' else '允许 npm 生命周期脚本')
                              if allow_scripts else '禁止安装脚本和源码构建'),
            'execution_environment': mode,
            'network': 'host_including_localhost' if local else network,
            'domains': config.COMMAND_NETWORK_ALLOWLIST if network and not local else [],
            'timeout_seconds': timeout,
            'runtime_profile': profile, 'memory_limit_mb': None if local else command_memory_limit_mb(profile),
            **({} if local else limits), 'workspace_limit_mb': None if local else config.WORKSPACE_LIMIT_MB,
            'command': command}, ensure_ascii=False)
        if not ask_permission('prepare_project_environment', plan, reason, command=command, force_confirmation=True):
            return '[已取消] 用户未确认，未安装依赖；真实构建/测试尚未完成。'
        # 审批后再次校验根与项目路径；脚本内还会复核清单与安装目标。
        if resolve_path(project_dir, write=True) != project:
            raise SandboxError('项目目录在确认期间发生变化，拒绝安装')
        with network_proxy if network_proxy is not None else nullcontext() as proxy:
            if proxy is not None:
                isolated = isolated_command(argv, network_socket=proxy.path, runtime_profile=profile,
                                            file_limit_mb=limits['file_limit_mb'], cpu_seconds=limits['cpu_seconds'])
            audit('environment_prepare_started', manager=manager, project_dir=project_dir,
                  manifest_sha256=fingerprints, network=network, allow_scripts=allow_scripts,
                  timeout_seconds=timeout, execution_environment=mode, **({} if local else limits))
            execute_options = ({'environment': local_process_environment(), 'check_quota': False,
                                'execution_mode': 'local'} if local else {'file_limit_mb': limits['file_limit_mb']})
            result = _execute(command, reason, argv, isolated, timeout=timeout, **execute_options)
            if proxy is not None and proxy.denial_summary():
                result += '\n[网络边界诊断] ' + proxy.denial_summary() + '；这是本地网关拒绝，不代表包仓库或依赖不存在。'
        audit('environment_prepare_finished', manager=manager, project_dir=project_dir,
              success=result.startswith('[退出码] 0\n'))
        return result + '\n[验证范围] 本操作仅准备依赖并验证运行时能执行，未执行项目 build/test。继续调用 run_command 运行真实构建和测试；失败时可能保留部分依赖，不自动删除用户文件。'
    except (SandboxError, ValueError, OSError) as exc:
        audit('blocked', action='prepare_project_environment', reason=str(exc))
        return f'[已拦截] {exc}；未完成真实构建/测试。'
