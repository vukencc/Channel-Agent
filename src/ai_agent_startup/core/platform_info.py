"""只读平台能力说明；找到启动器不等于真实隔离探测通过。"""
import os
import shutil
import sys


def platform_info():
    linux = sys.platform == 'linux'
    return {'platform': sys.platform, 'file_lock_backend': 'msvcrt' if os.name == 'nt' else 'flock' if os.name == 'posix' else None,
            'command_backend': 'bubblewrap' if linux else None,
            'launchers_found': {name: bool(shutil.which(name, path='/usr/bin:/bin')) if linux else False for name in ('bwrap', 'prlimit')},
            'isolation_verified': False,
            'verification': '运行 uv run python scripts/ci_sandbox_probe.py 实测；本命令只显示静态能力',
            'alternative': 'Linux/WSL2 安装 bubblewrap/util-linux；无隔离能力时仍可使用受支持文件工具与 --prompt headless',
            'limitations': ['非 Linux 命令及原子 move 拒绝执行', 'Windows/macOS 本轮无原生验收', 'Docker/Podman 后端仅设计，无宿主执行回退']}
