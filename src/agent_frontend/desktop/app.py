"""原生 Qt 桌面入口，不启动 HTTP 服务或浏览器。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def configure_fonts(app) -> None:
    """WSLg 未安装 CJK 字体时使用本机已有字体，不下载或复制字体。"""
    from PySide6.QtGui import QFont, QFontDatabase
    if QFontDatabase.families(QFontDatabase.WritingSystem.SimplifiedChinese):
        return
    if sys.platform == 'linux' and os.environ.get('WSL_DISTRO_NAME'):
        for path in (Path('/mnt/c/Windows/Fonts/msyh.ttc'), Path('/mnt/c/Windows/Fonts/simhei.ttf')):
            if path.is_file():
                identifier = QFontDatabase.addApplicationFont(str(path))
                families = QFontDatabase.applicationFontFamilies(identifier)
                if families:
                    app.setFont(QFont(families[0], 10))
                    return


def main() -> None:
    parser = argparse.ArgumentParser(description='Agent 原生桌面（Qt Widgets）')
    parser.add_argument('--state-dir', type=Path, help='独立会话状态目录；不能与其他进程共用')
    arguments = parser.parse_args()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        parser.exit(2, '原生桌面需要可选依赖：uv sync --locked --extra desktop\n')
    from agent_core import config
    from .runtime import RuntimeBridge
    from .window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Agent Desktop')
    app.setOrganizationName('ai-agent-startup')
    configure_fonts(app)
    bridge = RuntimeBridge(arguments.state_dir or Path(config.AGENT_STATE_DIR))
    window = MainWindow(bridge)
    window.show()
    bridge.start()
    try:
        app.exec()
    finally:
        # 正常关闭由窗口异步收尾；异常退出仍确保非守护线程开始排空。
        bridge.shutdown()
    if bridge.worker_alive:
        bridge._thread.join()
    # 原生控件在 QApplication 之前按 Qt 事件顺序销毁，避免解释器退出时乱序回收。
    from PySide6.QtCore import QCoreApplication, QEvent
    window.deleteLater()
    bridge.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    main()
