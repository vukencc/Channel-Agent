"""离线打包验收：真实 Core/Qt/依赖探测，不调用模型、不下载模型。"""
from __future__ import annotations

import builtins
from collections.abc import Callable
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any


Check = Callable[[Path], None]


def _require(value: Any) -> None:
    if not value:
        raise RuntimeError('验收断言失败')


def _wait_until(app: Any, predicate: Callable[[], bool], *, timeout: float = 10,
                cancelled: threading.Event | None = None) -> None:
    """事件泵每轮让出 Python 线程，超时或取消时明确失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise TimeoutError


@contextmanager
def _offline_environment():
    values = {'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
              'HF_HUB_DISABLE_TELEMETRY': '1'}
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_config_root(root: Path) -> None:
    from agent_core import config
    from agent_core.project_paths import project_root
    selected = Path(config.PROJECT_ROOT).resolve()
    override = os.environ.get('AI_AGENT_PROJECT_ROOT')
    if override:
        _require(selected == Path(override).expanduser().resolve())
    if getattr(sys, 'frozen', False) and not override:
        from agent_frontend.desktop.launcher import frozen_data_root
        expected = frozen_data_root(sys.platform)
        _require(selected == expected.resolve())
        _require(not selected.is_relative_to(Path(sys.executable).resolve().parent))
        _require(not selected.is_relative_to(Path(getattr(sys, '_MEIPASS', sys.executable)).resolve()))
    previous = Path.cwd()
    try:
        os.chdir(root)
        _require(project_root(config.__file__) == selected)
    finally:
        os.chdir(previous)


def _check_web_debug(root: Path) -> None:
    from agent_frontend.web import app as web
    from agent_test.web import router as debug
    assets = Path(web.__file__).with_name('static')
    debug_assets = Path(debug.__file__).parents[1] / 'static'
    for name in ('index.html', 'app.js', 'app.css', 'flow.js'):
        _require((assets / name).is_file() and (assets / name).stat().st_size > 0)
    for name in ('debug.html', 'debug.css', 'debug.js', 'debug-view.js'):
        _require((debug_assets / name).is_file() and (debug_assets / name).stat().st_size > 0)
    # 不启动服务或打开端口；构建真实路由表以发现遗漏导入。
    application = web.create_app(state_dir=root / 'web-state', token='self-check-local')
    _require(any(route.path == '/api/meta' for route in application.routes))


def _check_installation_boundary(root: Path) -> None:
    """成品安装位置不能被选为可修改项目，独立用户数据根不削弱源码保护。"""
    from types import SimpleNamespace
    from agent_core.core.workspaces import SessionWorkspaces
    manager = SimpleNamespace(sessions={}, store=SimpleNamespace(
        root=root / 'private-state', workspace_root=root / 'private-workspaces'))
    workspaces = SessionWorkspaces(manager)
    for path in (Path(sys.executable).resolve().parent, Path(sys._MEIPASS).resolve()):
        try:
            workspaces.validate_path(str(path))
        except PermissionError:
            continue
        raise RuntimeError('安装目录未受保护')


def _check_qt(root: Path) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from agent_frontend.desktop.runtime import RuntimeBridge
    from agent_frontend.desktop.window import MainWindow
    calls: list[bool] = []
    async def forbidden_model(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        raise RuntimeError('验收禁止模型调用')
    app = QApplication.instance() or QApplication([])
    bridge = RuntimeBridge(root / 'qt-state', workspace_root=root / 'qt-workspace', model=forbidden_model)
    errors: list[bool] = []
    bridge.error.connect(lambda _: errors.append(True))
    window = None
    try:
        window = MainWindow(bridge)
        window.show()
        bridge.start()
        _wait_until(app, lambda: bool(errors) or window.current_session is not None)
        _require(not errors and bridge.worker_alive and bridge.is_running and not calls)
    finally:
        bridge.shutdown()
        try:
            _wait_until(app, lambda: not bridge.worker_alive and not bridge.is_running)
        finally:
            if window is not None:
                window.close()
                window.deleteLater()
            bridge.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _require(not errors and not calls)


def _check_flow_template(root: Path) -> None:
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    state, workspace = root / 'flow-state', root / 'flow-workspace'
    store = SessionStore(state, workspace_root=workspace)
    try:
        service = FlowService(SessionManager(store))
        flow = service.create(title='打包验收')
        flow['nodes'] = [{'id': 'probe', 'title': '验收', 'prompt': '不执行模型', 'x': 0, 'y': 0}]
        saved = service.save(flow)
        payload = service.export_template(saved['id'])
        path = root / 'flow.template.json'
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        imported = service.import_template(json.loads(path.read_text(encoding='utf-8')))
        identifier = imported['id']
    finally:
        store.close()
    store = SessionStore(state, workspace_root=workspace)
    try:
        reloaded = FlowService(SessionManager(store)).get(identifier)
        _require(reloaded['nodes'][0]['prompt'] == '不执行模型')
    finally:
        store.close()


def _check_dynamic_tools(root: Path) -> None:
    """校验真实 schema 资源及便携定义往返，不运行工具源码。"""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError
    from agent_core.core.custom_tools import CustomToolSpec
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    definition = {
        'name': 'packaged_probe', 'description': '打包动态工具验收',
        'parameters': {'type': 'object', 'properties': {'count': {'type': 'integer', 'minimum': 1}},
                       'required': ['count'], 'additionalProperties': False},
        'code': "raise RuntimeError('验收不得执行源码')\ndef main(arguments):\n    return arguments\n",
        'network': False,
    }
    spec = CustomToolSpec.model_validate(definition)
    invalid = {**definition, 'parameters': {'type': 'object', 'properties': {
        'count': {'type': 'integer', 'minimum': 'invalid'}}}}
    try:
        CustomToolSpec.model_validate(invalid)
    except ValueError:
        pass
    else:
        raise RuntimeError('动态工具 schema 校验未拒绝无效定义')
    validator = Draft202012Validator(spec.parameters)
    validator.validate({'count': 1})
    try:
        validator.validate({'count': 0})
    except ValidationError:
        pass
    else:
        raise RuntimeError('动态工具参数校验未拒绝无效数据')

    state, workspace = root / 'dynamic-flow-state', root / 'dynamic-flow-workspace'
    store = SessionStore(state, workspace_root=workspace)
    try:
        service = FlowService(SessionManager(store))
        flow = service.create(title='动态工具打包验收')
        flow['nodes'] = [{'id': 'probe', 'title': '验收', 'prompt': '不执行模型',
                          'x': 0, 'y': 0, 'custom_tools': [definition],
                          'tool_names': ['packaged_probe']}]
        saved = service.save(flow)
        payload = service.export_template(saved['id'])
        path = root / 'dynamic-flow.template.json'
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        imported = service.import_template(json.loads(path.read_text(encoding='utf-8')))
        identifier = imported['id']
    finally:
        store.close()
    store = SessionStore(state, workspace_root=workspace)
    try:
        node = FlowService(SessionManager(store)).get(identifier)['nodes'][0]
        _require(node['custom_tools'] == [spec.model_dump()])
        _require('packaged_probe' in node['tool_names'])
    finally:
        store.close()


def _tool_context(root: Path, audit_path: Path, confirm: Callable[..., bool]):
    from agent_core.tools.sandbox import ToolContext
    root.mkdir(parents=True, exist_ok=True)
    status = root.stat()
    return ToolContext(root, audit_path, confirm, threading.Event(),
                       permission_policy='standard', execution_environment='sandbox',
                       root_identity=(status.st_dev, status.st_ino), flow_workspace_scope=True)


def _check_file_tools(root: Path) -> None:
    from agent_core.core.audit_writer import close_audit_handles
    from agent_core.tools.file_crud import create_file, read_file
    from agent_core.tools.sandbox import tool_context
    confirmations: list[bool] = []
    def approve(*args: Any) -> bool:
        confirmations.append(True)
        return True
    context = _tool_context(root / 'files', root / 'file-audit.jsonl', approve)
    try:
        with tool_context(context):
            create_file('probe.txt', '打包文件验收')
            _require('打包文件验收' in read_file('probe.txt'))
            _require((context.root / 'probe.txt').read_text(encoding='utf-8') == '打包文件验收')
            context.confirm = lambda *_: False
            create_file('denied.txt', '禁止写入')
            _require(not (context.root / 'denied.txt').exists())
        _require(confirmations)
    finally:
        close_audit_handles(root)


def _check_wsl(root: Path) -> None:
    from agent_core.core.audit_writer import close_audit_handles
    from agent_core.tools.command import run_command
    from agent_core.tools.sandbox import tool_context
    from agent_core.tools.wsl_command import _worker_script
    _require(os.name == 'nt' and _worker_script().is_file())
    context = _tool_context(root / 'wsl-files', root / 'wsl-audit.jsonl', lambda *_: True)
    try:
        with tool_context(context):
            result = run_command("printf packaged-wsl-ok > packaged-wsl.txt")
        _require('[退出码] 0' in result)
        _require((context.root / 'packaged-wsl.txt').read_text(encoding='utf-8') == 'packaged-wsl-ok')
        entries = [json.loads(line) for line in context.audit_path.read_text(encoding='utf-8').splitlines()]
        _require(any(row.get('event') == 'wsl_tool_audit' for row in entries))
    finally:
        close_audit_handles(root)


def _check_linux_sandbox(root: Path) -> None:
    """运行真实隔离命令，验证冻结文件和权限边界。"""
    from agent_core.core.audit_writer import close_audit_handles
    from agent_core.tools.command import run_command
    from agent_core.tools.sandbox import tool_context
    context = _tool_context(root / 'linux-files', root / 'linux-audit.jsonl', lambda *_: True)
    try:
        with tool_context(context):
            result = run_command("printf packaged-linux-ok > packaged-linux.txt")
        _require('[退出码] 0' in result)
        _require((context.root / 'packaged-linux.txt').read_text(encoding='utf-8') == 'packaged-linux-ok')
        _require(context.audit_path.is_file())
    finally:
        close_audit_handles(root)


def _check_numpy(root: Path) -> None:
    import numpy
    _require(int(numpy.array([2, 3]).sum()) == 5)


def _check_torch(root: Path) -> None:
    import torch
    tensor = torch.tensor([2, 3], device='cpu')
    _require(tensor.sum().item() == 5 and tensor.device.type == 'cpu')


def _check_onnxruntime(root: Path) -> None:
    import onnxruntime
    _require('CPUExecutionProvider' in onnxruntime.get_available_providers())


def _check_jieba(root: Path) -> None:
    import jieba
    tokenizer = jieba.Tokenizer()
    tokenizer.tmp_dir = str(root)
    _require('打包' in list(tokenizer.cut('打包验收', HMM=False)))


def _check_documents(root: Path) -> None:
    from docx import Document
    from pypdf import PdfReader, PdfWriter
    from PIL import Image
    document = Document()
    document.add_paragraph('打包验收')
    path = root / 'probe.docx'
    document.save(path)
    _require(Document(path).paragraphs[0].text == '打包验收')
    path = root / 'probe.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open('wb') as stream:
        writer.write(stream)
    with path.open('rb') as stream:
        _require(len(PdfReader(stream).pages) == 1)
    path = root / 'probe.png'
    with Image.new('RGB', (2, 2)) as image:
        image.save(path)
    with Image.open(path) as image:
        image.load()
        _require(image.size == (2, 2))


def _import_check(name: str) -> Check:
    def check(root: Path) -> None:
        importlib.import_module(name)
    return check


def _capabilities() -> dict[str, Check]:
    checks = {'config_root': _check_config_root, 'web_debug_resources': _check_web_debug,
              'qt_runtime': _check_qt, 'flow_template': _check_flow_template,
              'dynamic_tools': _check_dynamic_tools,
              'native_file_tools': _check_file_tools, 'numpy': _check_numpy,
              'torch_cpu': _check_torch, 'onnxruntime_cpu': _check_onnxruntime,
              'fastembed': _import_check('fastembed'), 'transformers': _import_check('transformers'),
              'sentence_transformers': _import_check('sentence_transformers'),
              'jieba_dictionary': _check_jieba, 'documents_vision': _check_documents}
    if os.environ.get('CHANNEL_AGENT_SELF_CHECK_WSL') == '1':
        checks['wsl_command'] = _check_wsl
    if getattr(sys, 'frozen', False) and sys.platform == 'linux':
        checks['linux_sandbox'] = _check_linux_sandbox
    if getattr(sys, 'frozen', False):
        checks['installation_boundary'] = _check_installation_boundary
    return checks


def _exception_class(exc: BaseException) -> str:
    """只允许内建类别，避免第三方自定义类别名携带敏感内容。"""
    kind = type(exc)
    return kind.__name__ if kind.__module__ == 'builtins' and getattr(builtins, kind.__name__, None) is kind else 'Exception'


def run(report_path: Path) -> int:
    """写入指定 JSON；能力失败、取消和报告写入失败均非零退出。"""
    report: dict[str, Any] = {'schema_version': 1, 'ok': True, 'capabilities': {}, 'errors': {}}
    exit_code = 0
    try:
        with tempfile.TemporaryDirectory(prefix='channel-packaged-check-') as temporary, _offline_environment():
            root = Path(temporary).resolve()
            for name, check in _capabilities().items():
                try:
                    check(root)
                except (Exception, KeyboardInterrupt) as exc:
                    report['capabilities'][name] = False
                    report['errors'][name] = _exception_class(exc)
                    report['ok'] = False
                    exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
                    if exit_code == 130:
                        break
                else:
                    report['capabilities'][name] = True
    except (Exception, KeyboardInterrupt) as exc:
        report['ok'] = False
        report['errors']['self_check'] = _exception_class(exc)
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    try:
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError:
        return 1
    return exit_code


if __name__ == '__main__':
    raise SystemExit(run(Path(sys.argv[1])) if len(sys.argv) == 2 else 2)

