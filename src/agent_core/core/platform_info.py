"""只读平台能力说明；找到启动器不等于真实隔离探测通过。"""
import os
from pathlib import Path
import shutil
import sys


def platform_info():
    linux = sys.platform == 'linux'
    windows = os.name == 'nt'
    if windows:
        return {'platform': sys.platform, 'file_lock_backend': 'msvcrt',
                'file_backend': 'windows-handles', 'command_backend': 'wsl2-bubblewrap',
                'launchers_found': {'wsl': (Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/wsl.exe').is_file()},
                'isolation_verified': False,
                'verification': '运行 python -m agent_test.scripts.ci_sandbox_probe 验证所选 WSL 的真实隔离能力',
                'alternative': '配置 WINDOWS_WSL_DISTRIBUTION 与 WINDOWS_WSL_PYTHON，参见 docs/windows.md',
                'limitations': ['命令使用 WSL 内的 POSIX Shell 与 Linux 工具链',
                                'Windows 本机命令、隔离后台命令与批量清理尚不可用',
                                '工作区仅支持本地磁盘目录，拒绝 UNC、设备路径和重解析点']}
    return {'platform': sys.platform, 'file_lock_backend': 'msvcrt' if os.name == 'nt' else 'flock' if os.name == 'posix' else None,
            'command_backend': 'bubblewrap' if linux else None,
            'launchers_found': {name: bool(shutil.which(name, path='/usr/bin:/bin')) if linux else False for name in ('bwrap', 'prlimit')},
            'isolation_verified': False,
            'verification': '运行 uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe 实测；本命令只显示静态能力',
            'alternative': 'Linux/WSL2 安装 bubblewrap/util-linux；无隔离能力时仍可使用受支持文件工具与 --prompt headless',
            'limitations': ['非 Linux 命令及原子 move 拒绝执行', 'Windows/macOS 本轮无原生验收', 'Docker/Podman 后端仅设计，无宿主执行回退']}
