"""构建 Windows 免安装应用、验收报告和带 SHA256 的 ZIP。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import marshal
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from types import CodeType
import zipfile


def source_root() -> Path:
    """构建仅针对源码工作区，不以启动目录或运行时配置推断源码。"""
    return Path(__file__).resolve().parents[3]


def file_manifest(directory: Path) -> list[dict]:
    """清单只记录成品文件与校验值，不包含本机路径和环境变量。"""
    result = []
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('分发目录不能包含符号链接')
        if not path.is_file() or path == directory / 'manifest.json':
            continue
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        result.append({'path': path.relative_to(directory).as_posix(),
                       'size': path.stat().st_size, 'sha256': digest})
    return result


def create_icon(path: Path) -> None:
    """将应用已有的四格标识绘成 Windows 多尺寸图标。"""
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 255, 255), radius=56, fill='#171c25')
    for x in (54, 142):
        for y in (54, 142):
            draw.rounded_rectangle((x, y, x + 59, y + 59), radius=16, fill='#a7c7fb')
    image.save(path, sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])


def windows_dependency_binaries(binaries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Qt 使用 Windows 自带 ICU，排除开发工具同名库和它独有的数据 DLL。"""
    versioned_icu = any(re.fullmatch(r'icu(?:uc|in|io)\d+\.dll', PureWindowsPath(entry[0]).name.casefold())
                        for entry in binaries)
    return [entry for entry in binaries
            if PureWindowsPath(entry[0]).name.casefold() != 'icuuc.dll'
            and (versioned_icu or not re.fullmatch(r'icudt\d+\.dll', PureWindowsPath(entry[0]).name.casefold()))]


def build_environment(root: Path, icon: Path) -> dict[str, str]:
    """只对构建子进程收窄工具搜索路径，保留系统和用户的原配置。"""
    system = Path(os.environ.get('SystemRoot', 'C:/Windows'))
    return {**os.environ, 'CHANNEL_AGENT_BUILD_ROOT': str(root),
            'CHANNEL_AGENT_BUILD_ICON': str(icon), 'PYTHONUTF8': '1',
            'PYTHON_DOTENV_DISABLED': '1',
            'PATH': os.pathsep.join([str(Path(sys.executable).parent), str(Path(sys.base_prefix)),
                                   str(system / 'System32'), str(system)])}


def check_build_dependencies() -> None:
    """构建前检查必需的打包和可选功能依赖，不修改环境。"""
    required = ('PyInstaller', 'PySide6', 'fastapi', 'docx', 'pypdf', 'PIL',
                'numpy', 'torch', 'onnxruntime', 'fastembed', 'transformers',
                'sentence_transformers', 'jieba')
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit('缺少构建依赖：' + ', '.join(missing) +
                         '\n请运行 uv sync --locked --group packaging --extra desktop --extra web --extra documents --extra vision --python 3.12')


def copy_dependency_licenses(destination: Path) -> None:
    """保留安装依赖的许可证和声明，不收集运行配置或开发目录。"""
    notices = destination / 'THIRD_PARTY_LICENSES'
    notices.mkdir(exist_ok=True)
    packages = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get('Name', 'unknown')
        # 分发名来自安装元数据，只允许普通目录名称。
        label = ''.join(character if character.isalnum() or character in '._-' else '_'
                        for character in f'{name}-{distribution.version}')
        included = []
        for relative in distribution.files or ():
            parts = relative.parts
            if any(part in ('.', '..') for part in parts) or relative.is_absolute():
                continue
            if not any(part.lower().startswith(('license', 'licence', 'copying', 'notice'))
                       for part in parts):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file() or source.suffix in ('.py', '.pyc', '.pyd', '.dll'):
                continue
            target = notices / label / Path(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            included.append(relative.as_posix())
        if included:
            packages.append({'name': name, 'version': distribution.version, 'notices': included})
    (notices / 'index.json').write_text(json.dumps(packages, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.copytree(source_root() / 'src/agent_test/packaging/windows/licenses',
                    notices / 'Qt-LGPL', dirs_exist_ok=True)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copyfile(python_license, notices / 'Python-LICENSE.txt')


def _run(arguments: list[str], *, cwd: Path, env: dict | None = None) -> None:
    subprocess.run(arguments, cwd=cwd, env=env, check=True)


def equivalent_code(first: object, second: object) -> bool:
    """忽略冻结器改写的文件名/行表，但严格比较指令、常量和参数信息。"""
    if isinstance(first, CodeType) and isinstance(second, CodeType):
        fields = ('co_code', 'co_names', 'co_varnames', 'co_freevars', 'co_cellvars',
                  'co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_flags',
                  'co_exceptiontable')
        return (all(getattr(first, field) == getattr(second, field) for field in fields)
                and len(first.co_consts) == len(second.co_consts)
                and all(equivalent_code(a, b) for a, b in zip(first.co_consts, second.co_consts)))
    return type(first) is type(second) and first == second


def verify_application_code(executable: Path) -> int:
    """拒绝混入构建期间编辑前的旧代码；不将源码路径写入成品报告。"""
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    python_archive = archive.open_embedded_archive('PYZ.pyz')
    root = source_root()
    count = 0
    for name in python_archive.toc:
        if name.split('.')[0] not in {'agent_core', 'agent_frontend', 'agent_test'}:
            continue
        path = root / 'src' / Path(*name.split('.'))
        path = path / '__init__.py' if path.is_dir() else path.with_suffix('.py')
        expected = compile(path.read_bytes(), str(path), 'exec', dont_inherit=True, optimize=0)
        if not equivalent_code(python_archive.extract(name), expected):
            raise RuntimeError(f'成品代码与当前源码不一致：{name}')
        count += 1
    entry = root / 'src/agent_frontend/desktop/launcher.py'
    expected = compile(entry.read_bytes(), str(entry), 'exec', dont_inherit=True, optimize=0)
    if not equivalent_code(marshal.loads(archive.extract('launcher')), expected):
        raise RuntimeError('成品启动器与当前源码不一致')
    return count + 1


def verify_onefile_resources(executable: Path, resources: Path) -> int:
    """核对单文件归档中的静态文件、WSL 源码及分发资源。"""
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    names = {name.replace('\\', '/'): name for name in archive.toc}
    root = source_root()
    expected: list[tuple[str, Path]] = []
    for package, relative in (('agent_frontend', 'desktop/assets'),
                              ('agent_frontend', 'web/static'), ('agent_test', 'static')):
        base = root / 'src' / package / relative
        expected.extend((f'{package}/{relative}/{path.name}', path)
                        for path in base.iterdir() if path.is_file())
    core = root / 'src/agent_core'
    expected.extend((f'wsl_runtime/agent_core/{path.relative_to(core).as_posix()}', path)
                    for path in core.rglob('*.py') if '__pycache__' not in path.parts and not path.is_symlink())
    expected.extend((f'portable_resources/{path.relative_to(resources).as_posix()}', path)
                    for path in resources.rglob('*') if path.is_file())
    for name, path in expected:
        if name not in names or archive.extract(names[name]) != path.read_bytes():
            raise RuntimeError(f'成品资源与当前源码不一致：{name}')
    return len(expected)


def _write_readme(destination: Path, version: str, layout: str = 'onefile') -> None:
    text = f'''ChannelAgent {version} · Windows x64 免安装版

一、打开应用
将 ChannelAgent.exe 放到本地盘可写目录并双击。无需安装 Windows Python。
单文件启动时运行库临时解压到系统临时目录，退出后由启动器清理。
首次打开后在左下角「模型管理」添加模型连接。
新建项目时选择您自己的项目目录；也可以先设计 Flow 模板，运行时再绑定项目。

二、您的数据
默认配置、会话、日志和缓存放在 exe 所在目录。
首次运行自动创建 .env；按需编辑已有文件，configuration.example.env 仅供参考。
若程序目录不可写，会提示使用用户目录、选择其他目录或取消；不需要管理员权限。
模型连接可在界面里管理。此包不附带任何用户凭据、会话、项目或本地模型权重。
搬移时请一并保留您的配置和数据。可通过 --data-dir 或 --use-user-data 选择其他数据根。

三、隔离命令（可选 WSL2）
Windows 负责桌面、Core、文件操作与 Flow；运行命令需要 WSL2 Linux 环境。
1. 在 WSL Ubuntu 中准备 Python 3.12 或以上及其 venv，并安装 bubblewrap、util-linux。
2. 例如已安装 Python 3.12 时：python3.12 -m venv "$HOME/.local/share/ChannelAgent/wsl-venv"
3. 使用该环境的 pip 安装 WSL/requirements.txt（先转换为此文件的 /mnt/... 路径）：
   "$HOME/.local/share/ChannelAgent/wsl-venv/bin/python" -m pip install --require-hashes -r "/mnt/.../WSL/requirements.txt"
4. 在用户数据目录的 .env 设置 WINDOWS_WSL_DISTRIBUTION=Ubuntu，
   WINDOWS_WSL_PYTHON=/home/您的用户名/.local/share/ChannelAgent/wsl-venv/bin/python
   然后重启。系统需允许 Bubblewrap 所需的命名空间。
命令仍遵守权限、确认和审计；隔离不可用时拒绝执行，不自动回退。
原生 local 命令、后台命令和批量维护清理尚不可用。

四、知识库
运行库已包含本地检索和重排所需的 Python 依赖；模型权重不随应用分发。
本地 RAG 首次使用可能下载模型，或者在 .env 配置已有模型/云端 Embedding 服务。
不会在应用启动或自检时下载模型。

五、自检
在 PowerShell 执行 .\\ChannelAgent.exe --self-check "D:\\检查结果.json"
自检使用临时数据，不请求模型服务。需要测试 WSL 时先设置 CHANNEL_AGENT_SELF_CHECK_WSL=1。
manifest.json 提供文件校验值；程序尚未进行商业代码签名。
'''
    if layout == 'onedir':
        text = text.replace('将 ChannelAgent.exe 放到本地盘可写目录并双击。',
                            '将整个文件夹放到本地盘可写目录，保留 _internal 并双击 ChannelAgent.exe。')
        text = text.replace('单文件启动时运行库临时解压到系统临时目录，退出后由启动器清理。', '')
    (destination / '使用说明.txt').write_text(text, encoding='utf-8-sig')


def build(output_dir: Path, *, uv: str, smoke: bool = True, layout: str = 'onefile') -> Path:
    if sys.platform != 'win32':
        raise SystemExit('Windows 成品必须使用 Windows 原生 Python 构建。')
    if layout not in ('onefile', 'onedir'):
        raise ValueError('无效的 Windows 构建布局')
    check_build_dependencies()
    root = source_root()
    report_parent = root / '.cache' / 'reports'
    report_parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='windows-package-', dir=report_parent))
    icon = work / 'ChannelAgent.ico'
    create_icon(icon)
    environment = build_environment(root, icon)
    resources = work / 'portable_resources'
    resources.mkdir()
    prepare_resources(resources, uv=uv, layout=layout)
    environment['CHANNEL_AGENT_BUILD_LAYOUT'] = layout
    environment['CHANNEL_AGENT_BUILD_RESOURCES'] = str(resources)
    spec = root / 'src/agent_test/packaging/windows/ChannelAgent.spec'
    _run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
          '--distpath', str(work / 'dist'), '--workpath', str(work / 'build'), str(spec)],
         cwd=root, env=environment)
    bundle = work / 'dist' / ('ChannelAgent.exe' if layout == 'onefile' else 'ChannelAgent')
    return assemble(bundle, output_dir, work=work, uv=uv, smoke=smoke, resources=resources)


def prepare_resources(destination: Path, *, uv: str, layout: str = 'onefile') -> None:
    """构建前生成随 exe 内嵌且同时提供给分发包的资源。"""
    root = source_root()
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    shutil.copyfile(root / '.env.example', destination / 'configuration.example.env')
    if (root / 'LICENSE').is_file():
        shutil.copyfile(root / 'LICENSE', destination / 'LICENSE.txt')
    copy_dependency_licenses(destination)
    _write_readme(destination, version, layout)
    wsl = destination / 'WSL'
    wsl.mkdir()
    subprocess.run([uv, 'export', '--locked', '--package', 'ai-agent-core', '--no-dev',
                    '--no-emit-workspace', '--emit-index-url', '--format', 'requirements-txt',
                    '--output-file', str(wsl / 'requirements.txt')],
                   cwd=root, check=True, stdout=subprocess.DEVNULL)


def assemble(bundle: Path, output_dir: Path, *, work: Path, uv: str, smoke: bool = True,
             resources: Path | None = None) -> Path:
    """把冻结结果整理为可分发成品；实际 exe 验收通过后才生成 ZIP。"""
    root = source_root()
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    name = f'ChannelAgent-{version}-windows-x64-{stamp}'
    destination = output_dir / name
    if destination.exists():
        raise FileExistsError('目标成品目录已存在，请选择其他输出目录。')
    if bundle.is_file():
        destination.mkdir()
        shutil.copyfile(bundle, destination / 'ChannelAgent.exe')
    else:
        shutil.copytree(bundle, destination)
    verified_modules = verify_application_code(destination / 'ChannelAgent.exe')
    if resources is None:
        resources = work / 'portable_resources'
        resources.mkdir()
        prepare_resources(resources, uv=uv)
    verified_resources = verify_onefile_resources(destination / 'ChannelAgent.exe', resources) if bundle.is_file() else None
    shutil.copytree(resources, destination, dirs_exist_ok=True)
    executable = destination / 'ChannelAgent.exe'
    with executable.open('rb') as stream:
        exe_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    (destination / 'ChannelAgent.exe.sha256').write_text(f'{exe_digest}  ChannelAgent.exe\n', encoding='utf-8')
    if smoke:
        smoke_environment = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1', 'QT_QPA_PLATFORM': 'offscreen',
                             'AI_AGENT_PROJECT_ROOT': str(work / 'self-check-user'),
                             'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
        # 从任意目录启动，并移除开发 Python/工具 PATH，检查是否有源码或宿主依赖。
        smoke_environment.pop('PYTHONPATH', None)
        smoke_environment.pop('PYTHONHOME', None)
        smoke_environment['PATH'] = os.pathsep.join([
            str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32'),
            os.environ.get('SystemRoot', 'C:/Windows')])
        check = work / 'self-check.json'
        process = subprocess.run([str(destination / 'ChannelAgent.exe'), '--self-check', str(check)],
                                 cwd=work, env=smoke_environment, timeout=600)
        if process.returncode or not check.is_file():
            raise RuntimeError(f'成品自检失败，诊断目录：{work}')
        result = json.loads(check.read_text(encoding='utf-8'))
        if not result.get('ok'):
            raise RuntimeError(f'成品自检未通过，诊断目录：{work}')
        shutil.copyfile(check, destination / 'self-check.json')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True))
    manifest = {'application': 'ChannelAgent', 'version': version, 'platform': 'windows-x64',
                'built_at': datetime.now(timezone.utc).isoformat(),
                'source_revision': revision, 'working_tree_modified': dirty,
                'python': sys.version.split()[0],
                'pyinstaller': importlib.metadata.version('pyinstaller'),
                'verified_application_modules': verified_modules,
                'verified_onefile_resources': verified_resources,
                'files': file_manifest(destination)}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    archive = output_dir / f'{name}.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(output_dir).as_posix())
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(f'成品目录：{destination}\n分发压缩包：{archive}\n构建报告：{work}')
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=source_root() / 'dist')
    parser.add_argument('--uv', default=shutil.which('uv') or 'uv')
    parser.add_argument('--layout', choices=('onefile', 'onedir'), default='onefile')
    parser.add_argument('--skip-self-check', action='store_true', help='仅用于定位构建问题，不得作为验收通过发布')
    arguments = parser.parse_args()
    build(arguments.output_dir, uv=arguments.uv, smoke=not arguments.skip_self_check, layout=arguments.layout)


if __name__ == '__main__':
    main()
