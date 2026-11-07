"""在 Linux x64 构建、离线自检并归档免安装桌面成品。"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

from agent_test.scripts.build_windows import (source_root, verify_application_code,
                                               file_manifest, copy_dependency_licenses)


def build(output_dir: Path, *, bundle: Path | None = None) -> Path:
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise SystemExit('Linux 成品必须在 Linux x64 上构建。')
    root = source_root()
    reports = root / '.cache/reports'
    reports.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='linux-package-', dir=reports))
    env = {**os.environ, 'CHANNEL_AGENT_BUILD_ROOT': str(root), 'PYTHON_DOTENV_DISABLED': '1'}
    if bundle is None:
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(work / 'dist'),
                        '--workpath', str(work / 'build'), str(root / 'src/agent_test/packaging/linux/ChannelAgent.spec')],
                       cwd=root, env=env, check=True)
        bundle = work / 'dist/ChannelAgent'
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    name = f"ChannelAgent-{version}-linux-x64-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / name
    shutil.copytree(bundle, destination)
    executable = destination / 'ChannelAgent'
    count = verify_application_code(executable)
    for script in ('network_relay.py', 'runtime_paths.py'):
        path = destination / '_internal/agent_core/tools' / script
        if path.read_bytes() != (root / 'src/agent_core/tools' / script).read_bytes():
            raise RuntimeError(f'沙箱辅助脚本不一致：{script}')
        if script == 'runtime_paths.py':
            path.chmod(0o755)
    shutil.copyfile(root / '.env.example', destination / 'configuration.example.env')
    shutil.copyfile(root / 'LICENSE', destination / 'LICENSE.txt')
    copy_dependency_licenses(destination)
    libc_name, libc_version = platform.libc_ver()
    if libc_name != 'glibc' or not libc_version:
        raise RuntimeError('无法确定构建系统的 glibc 基线')
    (destination / '使用说明.txt').write_text(
        f'ChannelAgent {version} Linux x64\n运行 ./ChannelAgent；保留 _internal 目录。\n'
        f'构建系统 glibc {libc_version}；运行系统需兼容该版本及图形系统和 Qt 系统库。\n'
        '隔离命令需要 bubblewrap、util-linux、/usr/bin/python3 和允许用户命名空间的系统。\n'
        '配置和数据默认位于 $XDG_DATA_HOME/ChannelAgent 或 ~/.local/share/ChannelAgent。\n'
        '模型权重、用户配置、会话和项目不随包分发。\n', encoding='utf-8')
    check = work / 'self-check.json'
    smoke_env = {**env, 'AI_AGENT_PROJECT_ROOT': str(work / 'user'), 'QT_QPA_PLATFORM': 'offscreen',
                 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PATH': '/usr/bin:/bin'}
    for key in ('PYTHONPATH', 'PYTHONHOME', 'LD_LIBRARY_PATH'):
        smoke_env.pop(key, None)
    subprocess.run([str(executable), '--self-check', str(check)], cwd=work, env=smoke_env,
                   check=True, timeout=600)
    result = json.loads(check.read_text(encoding='utf-8'))
    if not result.get('ok'):
        raise RuntimeError(f'成品自检失败：{work}')
    shutil.copyfile(check, destination / 'self-check.json')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True))
    manifest = {'application': 'ChannelAgent', 'version': version, 'platform': 'linux-x64',
                'built_at': datetime.now(timezone.utc).isoformat(), 'source_revision': revision,
                'working_tree_modified': dirty, 'python': sys.version.split()[0],
                'build_libc': f'{libc_name} {libc_version}',
                'pyinstaller': importlib.metadata.version('pyinstaller'),
                'verified_application_modules': count, 'files': file_manifest(destination)}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    archive = output_dir / f'{name}.tar.gz'
    with tarfile.open(archive, 'x:gz') as output:
        output.add(destination, arcname=name)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    Path(str(archive) + '.sha256').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(f'成品：{archive}；报告：{work}')
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=source_root() / 'dist')
    parser.add_argument('--bundle', type=Path, help='仅重验和重新归档已有冻结目录')
    arguments = parser.parse_args()
    build(arguments.output_dir, bundle=arguments.bundle)


if __name__ == '__main__':
    main()

