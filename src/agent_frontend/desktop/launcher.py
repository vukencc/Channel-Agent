"""打包桌面入口，在导入运行时前设置用户状态根目录。"""
import multiprocessing
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile


def frozen_data_root(platform: str) -> Path:
    """Windows 使用程序目录；Linux 忽略非绝对 XDG 路径。"""
    if platform == 'win32':
        return Path(sys.executable).parent
    xdg = os.environ.get('XDG_DATA_HOME', '')
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / '.local' / 'share') / 'ChannelAgent'


def portable_user_root() -> Path:
    local = os.environ.get('LOCALAPPDATA')
    return (Path(local) if local else Path.home() / 'AppData' / 'Local') / 'ChannelAgentPortable'


def choose_portable_root(error) -> Path | None:
    """只有写入失败才让用户选择其他位置。"""
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    app = QApplication.instance() or QApplication(sys.argv[:1])
    box = QMessageBox()
    box.setWindowTitle('ChannelAgent')
    box.setText(f'无法{error.operation}：\n{error.path}')
    user = box.addButton('使用用户目录', QMessageBox.ButtonRole.AcceptRole)
    choose = box.addButton('选择目录', QMessageBox.ButtonRole.ActionRole)
    box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is user:
        return portable_user_root()
    if box.clickedButton() is choose:
        path = QFileDialog.getExistingDirectory(None, '选择数据目录')
        return Path(path) if path else None
    return None


def _write_info(path: Path, payload: dict) -> None:
    """报告仅能原子发布到不存在的安全路径。"""
    from agent_core.core.files import opened_path
    path = Path(os.path.abspath(path))
    temporary = None
    try:
        with opened_path(path.parent, path.parent, directory=True):
            fd, name = tempfile.mkstemp(prefix='.portable-info-', dir=path.parent)
            temporary = Path(name)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == 'nt':
                from agent_core.core.windows_security import protect_private_path
                from agent_core.core.windows_files import rename_noreplace
                protect_private_path(temporary)
                rename_noreplace(temporary, path, file_only=True)
            else:
                os.link(temporary, path, follow_symlinks=False)
                temporary.unlink()
            temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _portable_bootstrap() -> None:
    from agent_frontend.desktop.portable import PortableError, configure_environment, initialize_layout, install_packaged_resources
    parser = argparse.ArgumentParser(add_help=False)
    roots = parser.add_mutually_exclusive_group()
    roots.add_argument('--data-dir', type=Path)
    roots.add_argument('--use-user-data', action='store_true')
    parser.add_argument('--portable-info', type=Path)
    args, remaining = parser.parse_known_args(sys.argv[1:])
    sys.argv = [sys.argv[0], *remaining]
    noninteractive = args.portable_info is not None or '--self-check' in remaining
    allow_selection = (args.data_dir is None and not args.use_user_data
                       and not os.environ.get('AI_AGENT_PROJECT_ROOT', '').strip())
    root = (args.data_dir if args.data_dir is not None else portable_user_root() if args.use_user_data
            else Path(os.environ['AI_AGENT_PROJECT_ROOT']) if os.environ.get('AI_AGENT_PROJECT_ROOT', '').strip()
            else Path(sys.executable).parent)
    while True:
        try:
            layout = initialize_layout(root)
            install_packaged_resources(layout, Path(getattr(sys, '_MEIPASS', '')) / 'portable_resources')
            configure_environment(layout)
            if args.portable_info is not None:
                paths = {key: str(value) for key, value in asdict(layout).items() if key != 'root'}
                try:
                    _write_info(args.portable_info, {'root': str(layout.root), 'paths': paths})
                except (OSError, ValueError):
                    raise SystemExit(1) from None
                raise SystemExit(0)
            return
        except PortableError as exc:
            if args.portable_info is not None:
                try:
                    _write_info(args.portable_info, {'error': {'path': str(exc.path), 'operation': exc.operation, 'type': exc.kind}})
                except (OSError, ValueError):
                    pass
            elif not noninteractive and allow_selection and exc.kind == 'unwritable':
                selected = choose_portable_root(exc)
                if selected is not None:
                    root = selected
                    continue
            elif not noninteractive:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication.instance() or QApplication(sys.argv[:1])
                QMessageBox.critical(None, 'ChannelAgent', f'启动失败：{exc.operation}\n{exc.path}\n{exc.kind}')
            raise SystemExit(1) from None


def main() -> None:
    multiprocessing.freeze_support()
    for name, mode in (('stdin', 'r'), ('stdout', 'w'), ('stderr', 'w')):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, mode, encoding='utf-8'))
    if getattr(sys, 'frozen', False) and sys.platform == 'win32':
        _portable_bootstrap()
    elif getattr(sys, 'frozen', False):
        if not os.environ.get('AI_AGENT_PROJECT_ROOT'):
            os.environ['AI_AGENT_PROJECT_ROOT'] = str(frozen_data_root(sys.platform))
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check':
        if not (getattr(sys, 'frozen', False) and sys.platform == 'win32'):
            from agent_test.scripts.packaged_smoke import run
            raise SystemExit(run(Path(sys.argv[2])))
        # 自检输出不得覆盖已有文件、链接或应用状态。
        if os.path.lexists(sys.argv[2]):
            raise SystemExit(1)
        from agent_test.scripts.packaged_smoke import run
        try:
            with tempfile.TemporaryDirectory(prefix='channel-check-report-') as directory:
                temporary_report = Path(directory) / 'report.json'
                code = run(temporary_report)
                if not temporary_report.is_file():
                    raise SystemExit(code or 1)
                _write_info(Path(sys.argv[2]), json.loads(temporary_report.read_text(encoding='utf-8')))
                raise SystemExit(code)
        except (OSError, ValueError):
            raise SystemExit(1) from None
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
