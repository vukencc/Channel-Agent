"""Linux x64 桌面冻结配置；仅包含明确的应用资源和沙箱辅助脚本。"""
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(os.environ['CHANNEL_AGENT_BUILD_ROOT'])
datas = []
for package, patterns in (('agent_frontend', ['desktop/assets/*', 'web/static/*']),
                          ('agent_test', ['static/*'])):
    datas += collect_data_files(package, includes=patterns)
for name in ('network_relay.py', 'runtime_paths.py'):
    datas.append((str(root / 'src/agent_core/tools' / name), 'agent_core/tools'))
hiddenimports = collect_submodules('agent_core') + collect_submodules('agent_frontend')
hiddenimports += collect_submodules('agent_test.web')
hiddenimports += ['agent_test.runtime', 'agent_test.tool', 'agent_test.cli',
                  'agent_test.scripts.packaged_smoke', 'PySide6.QtSvg',
                  'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
                  'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']
for distribution in ('ai-agent-core', 'ai-agent-frontend', 'ai-agent-test',
                     'sentence-transformers', 'transformers', 'fastembed', 'torch',
                     'onnxruntime', 'huggingface-hub', 'tokenizers', 'tavily-python'):
    datas += copy_metadata(distribution, recursive=True)
a = Analysis([str(root / 'src/agent_frontend/desktop/launcher.py')],
             pathex=[str(root / 'src')], binaries=[], datas=datas, hiddenimports=hiddenimports,
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['pytest', 'onnx', 'pyarrow', 'IPython', 'jupyter', 'notebook',
                       'matplotlib', 'tensorboard', 'tensorflow', 'keras', 'tkinter',
                       'PyQt5', 'PyQt6', 'PySide2'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ChannelAgent',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ChannelAgent')
