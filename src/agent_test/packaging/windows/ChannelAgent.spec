# -*- mode: python ; coding: utf-8 -*-
"""Windows 免安装桌面包；仅收集明确的代码和资源，不读取运行数据。"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from agent_test.scripts.build_windows import windows_dependency_binaries

root = Path(os.environ['CHANNEL_AGENT_BUILD_ROOT'])
icon = os.environ['CHANNEL_AGENT_BUILD_ICON']

layout = os.environ.get('CHANNEL_AGENT_BUILD_LAYOUT', 'onefile')
if layout not in ('onefile', 'onedir'):
    raise ValueError('无效的 Windows 构建布局')
resource_root = Path(os.environ['CHANNEL_AGENT_BUILD_RESOURCES'])
datas = [(str(path), str(Path('portable_resources') / path.relative_to(resource_root).parent))
         for path in sorted(resource_root.rglob('*')) if path.is_file()]
for package, patterns in (
    ('agent_frontend', ['desktop/assets/*', 'web/static/*']),
    ('agent_test', ['static/*']),
):
    datas += collect_data_files(package, includes=patterns)

# Linux Python 不能导入 PYZ 中的代码或 Windows 扩展，单独附带 Core 的纯源码。
core = root / 'src' / 'agent_core'
for path in sorted(core.rglob('*.py')):
    if '__pycache__' not in path.parts and not path.is_symlink():
        datas.append((str(path), str(Path('wsl_runtime/agent_core') / path.relative_to(core).parent)))

hiddenimports = collect_submodules('agent_core') + collect_submodules('agent_frontend')
hiddenimports += collect_submodules('agent_test.web')
hiddenimports += ['agent_test.runtime', 'agent_test.tool', 'agent_test.cli',
                  'agent_test.scripts.packaged_smoke', 'PySide6.QtSvg',
                  'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
                  'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']

# 模型框架在运行时查询发行元数据；源码包的元数据也一并保留。
for distribution in ('ai-agent-core', 'ai-agent-frontend', 'ai-agent-test',
                     'sentence-transformers', 'transformers', 'fastembed', 'torch',
                     'onnxruntime', 'huggingface-hub', 'tokenizers', 'tavily-python'):
    datas += copy_metadata(distribution, recursive=True)

a = Analysis(
    [str(root / 'src/agent_frontend/desktop/launcher.py')],
    pathex=[str(root / 'src')],
    binaries=[], datas=datas, hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['pytest', 'onnx', 'pyarrow', 'IPython', 'jupyter', 'notebook',
              'matplotlib', 'tensorboard', 'tensorflow', 'keras', 'tkinter',
              'PyQt5', 'PyQt6', 'PySide2'],
    noarchive=False,
)
a.binaries = windows_dependency_binaries(a.binaries)
pyz = PYZ(a.pure)
if layout == 'onefile':
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, name='ChannelAgent',
        debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        console=False, disable_windowed_traceback=True, icon=icon,
    )
else:
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True, name='ChannelAgent',
        debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        console=False, disable_windowed_traceback=True, icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ChannelAgent')
