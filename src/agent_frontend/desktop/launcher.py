"""打包桌面入口，在导入运行时前设置用户状态根目录。"""
import multiprocessing
import os
from pathlib import Path
import sys


def frozen_data_root(platform: str) -> Path:
    """按平台选择独立用户数据根，忽略非绝对 XDG 路径。"""
    if platform == 'win32':
        local = os.environ.get('LOCALAPPDATA')
        return (Path(local) if local else Path.home() / 'AppData' / 'Local') / 'ChannelAgent'
    xdg = os.environ.get('XDG_DATA_HOME', '')
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / '.local' / 'share') / 'ChannelAgent'


def main() -> None:
    multiprocessing.freeze_support()
    for name, mode in (('stdin', 'r'), ('stdout', 'w'), ('stderr', 'w')):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, mode, encoding='utf-8'))
    if getattr(sys, 'frozen', False):
        if not os.environ.get('AI_AGENT_PROJECT_ROOT'):
            os.environ['AI_AGENT_PROJECT_ROOT'] = str(frozen_data_root(sys.platform))
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check':
        from agent_test.scripts.packaged_smoke import run
        raise SystemExit(run(Path(sys.argv[2])))
    try:
        from agent_frontend.desktop.app import main as desktop_main
        desktop_main()
    except Exception as exc:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv[:1])
        QMessageBox.critical(None, 'ChannelAgent', f'启动失败：{type(exc).__name__}')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
