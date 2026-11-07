"""Windows 离屏渲染也应能显示系统已有的中文字体。"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='验证 Windows 系统字体')
def test_windows_offscreen_loads_available_system_cjk_font():
    fonts = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts'
    if not any((fonts / name).is_file() for name in ('msyh.ttc', 'simhei.ttf')):
        pytest.skip('此 Windows 安装未提供中文系统字体')
    script = '''
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontMetrics
from agent_frontend.desktop.app import configure_fonts
app = QApplication([])
configure_fonts(app)
assert QFontMetrics(app.font()).inFontUcs4(0x4e2d)
'''
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
