"""Qt 与 Core 的线程边界：只有后台事件循环持有可变运行时。"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from agent_core.api import LazyRecord, SessionManager, SessionStore, config, open_session
from agent_core.core.headless import redact_result
from agent_core.core.flows import FlowService
from agent_core.core.llm import call_model, close_clients
from agent_core.core.permissions import public_permission_policy
from agent_core.tools.sandbox import ToolContext, audit, tool_context
from agent_frontend.bootstrap import configure_debug_tools, create_observer


class RuntimeBridge(QObject):
    """信号只传递 JSON 字符串，禁止把 Core Session 引用交给 Qt。"""

    state_changed = Signal(str)
    error = Signal(str)
    finished = Signal(str, str)
    stopped = Signal()

    def __init__(self, state_dir: Path, *, workspace_root: Path | None = None,
                 model: Callable = call_model):
        super().__init__()
        self.state_dir = Path(state_dir)
        self.workspace_root = Path(workspace_root or config.SANDBOX_DIR)
        self._model = model
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.is_running = False
        self._closing = False
        self._selected: str | None = None
        self._selected_flow: str | None = None
        self._flows: FlowService | None = None
        self._page_sizes: dict[str, int] = {}
        self._dirty = False
        self._last_payload = ''
        self._manager: SessionManager | None = None
        self._store: SessionStore | None = None
        self._stop: asyncio.Event | None = None
        self._mutations: asyncio.Lock | None = None
        self._commands: set[asyncio.Task] = set()
        self._secrets: set[str] = set()

    @property
    def worker_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run, name='desktop-core', daemon=False)
        self._thread.start()

    def request(self, action: str, **arguments: Any) -> None:
        """调用方不等待 Future，所有完成和错误经排队信号返回。"""
        loop = self._loop
        if self._closing or loop is None or not self.is_running:
            return
        loop.call_soon_threadsafe(self._schedule_command, action, arguments)

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        loop = self._loop
        if loop is not None and self._stop is not None:
            loop.call_soon_threadsafe(self._stop.set)

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            # 未知异常只展示类别，避免底层响应或 URL 泄漏凭据。
            if isinstance(exc, RuntimeError) and str(exc).startswith('该状态目录已有 CLI'):
                self.error.emit('状态目录已被其他进程占用；请关闭原进程，或用 --state-dir 指定独立目录。')
            else:
                self.error.emit(f'桌面运行时无法启动（{type(exc).__name__}）；请检查状态目录与可选依赖。')
        finally:
            self._loop = None
            self.is_running = False
            self.stopped.emit()

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._mutations = asyncio.Lock()
        configure_debug_tools()
        self._store = await asyncio.to_thread(SessionStore, self.state_dir, workspace_root=self.workspace_root)
        try:
            self._manager = SessionManager(self._store, self._notify, self._model,
                                           cascade_deletions=True, observer=create_observer())
            self._flows = FlowService(self._manager)
            if not self._manager.sessions:
                value = open_session(self._manager)
                self._selected = value.id
            else:
                self._selected = next(iter(self._manager.sessions))
            await self._materialize(self._selected)
            self._dirty = True
            if self._closing:
                self._stop.set()
            lifecycle = None
            while not self._stop.is_set():
                # Core 的最后一次保存可能早于 task.done；终态不得依赖下一次外部通知。
                current = tuple((value.id, value.busy, len(value.tool_workers), value.record['status'])
                                for value in self._manager.sessions.values())
                if self._dirty or current != lifecycle:
                    self._publish()
                    lifecycle = current
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.08)
                except TimeoutError:
                    pass
        finally:
            self._closing = True
            # 先让当前可信用户操作结束，再取消 Agent 并排空工具和 writer。
            if self._commands:
                await asyncio.gather(*list(self._commands), return_exceptions=True)
            try:
                if self._manager is not None:
                    try:
                        if self._flows is not None:
                            await self._flows.shutdown()
                    finally:
                        await self._manager.shutdown()
            finally:
                try:
                    await close_clients()
                finally:
                    await asyncio.to_thread(self._store.close)

    def _notify(self) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._mark_dirty)

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _schedule_command(self, action: str, arguments: dict) -> None:
        if self._closing:
            return
        task = asyncio.create_task(self._command(action, arguments))
        self._commands.add(task)
        task.add_done_callback(self._commands.discard)

    async def _materialize(self, identifier: str | None) -> None:
        if identifier is None:
            return
        record = self._manager.sessions[identifier].record
        if isinstance(record, LazyRecord) and record.loader is not None:
            await asyncio.to_thread(record.materialize)

    def _clean(self, value: Any) -> Any:
        cleaned = redact_result(value)
        # Core 的通用脱敏覆盖环境密钥；桌面再覆盖私有运行时连接。
        if self._manager is not None:
            for identifier in self._manager.model_registry.ids():
                endpoint = self._manager.model_registry.resolve(identifier)
                if endpoint is not None and len(endpoint.api_key) >= 8:
                    self._secrets.add(endpoint.api_key)

        def clean(item: Any) -> Any:
            if isinstance(item, str):
                for secret in self._secrets:
                    item = item.replace(secret, '[已隐藏配置密钥]')
                return item
            if isinstance(item, dict):
                return {key: clean(child) for key, child in item.items()}
            if isinstance(item, list):
                return [clean(child) for child in item]
            return item
        return clean(cleaned)

    def _publish(self) -> None:
        self._dirty = False
        manager = self._manager
        rows = []
        for value in manager.sessions.values():
            record = value.record
            lazy = isinstance(record, LazyRecord) and record.loader is not None
            rows.append({'id': value.id, 'title': record['title'], 'busy': value.busy,
                         'flow_id': record.get('flow_id'), 'flow_node_id': record.get('flow_node_id'),
                         'status': record['status'], 'phase': value.phase,
                         'parent_id': record.get('delegated_from'),
                         'tool_workers': len(value.tool_workers),
                         'total_messages': record.get('message_count', 0) if lazy else len(record['messages']),
                         'confirmation': copy.deepcopy(value.confirmation),
                         'permission_policy': record.get('permission_policy') or config.TOOL_PERMISSION_POLICY,
                         'permission_label': public_permission_policy(record.get('permission_policy') or config.TOOL_PERMISSION_POLICY),
                         'model_profile': record.get('model_profile'),
                         'execution_environment': manager.execution_environment(value),
                         **manager.workspaces.metadata(value)})
        selected = next((row.copy() for row in rows if row['id'] == self._selected), None)
        if selected is not None:
            value = manager.sessions[self._selected]
            record = value.record
            if isinstance(record, LazyRecord) and record.loader is not None:
                selected.update(messages=[], next_before=record.get('message_count', 0), partial='')
            else:
                messages = record['messages']
                start = max(0, len(messages) - self._page_sizes.get(value.id, 60))
                selected.update(messages=copy.deepcopy(messages[start:]), next_before=start, partial=value.partial,
                                error='运行失败；请检查模型连接与任务设置。' if record.get('status') == 'error' else '')
        flow_records = self._flows.list() if self._flows is not None else []
        flows = [{'id': row['id'], 'title': row['title'], 'status': (row.get('run') or {}).get('status', 'idle'),
                  'node_count': len(row['nodes'])} for row in flow_records]
        selected_flow = next((row for row in flow_records if row['id'] == self._selected_flow), None)
        payload = json.dumps(self._clean({'sessions': rows, 'selected': selected,
                             'flows': flows, 'selected_flow': selected_flow,
                             'models': manager.model_registry.list_public(), 'default_model': config.MODEL,
                             'runtime': manager.concurrency_status(), 'load_errors': len(self._store.errors)}), ensure_ascii=False)
        if payload != self._last_payload:
            self._last_payload = payload
            self.state_changed.emit(payload)

    def _idle(self, value) -> None:
        if value.busy or value.tool_workers:
            raise ValueError('请等待会话与工具结束后修改设置。')

    def _audit(self, event: str, **fields: Any) -> None:
        manager = self._manager
        context = ToolContext(manager.store.workspace_root, manager.store.root / 'audit.jsonl',
                              lambda *_: False, threading.Event())
        with tool_context(context):
            audit(event, source='desktop_user', **fields)

    async def _command(self, action: str, arguments: dict) -> None:
        try:
            async with self._mutations:
                result = await self._dispatch(action, arguments)
                self.finished.emit(action, json.dumps(self._clean(result or {}), ensure_ascii=False))
                self._dirty = True
        except Exception as exc:
            if isinstance(exc, (ValueError, PermissionError, FileNotFoundError)):
                message = self._clean(str(exc))
            else:
                message = f'操作未完成（{type(exc).__name__}），请检查状态与设置。'
            self.error.emit(message)
            self.finished.emit(action, json.dumps({'error': True}))
            self._dirty = True

    async def _dispatch(self, action: str, arguments: dict) -> dict | None:
        manager = self._manager
        identifier = arguments.get('session_id', self._selected)
        value = manager.sessions.get(identifier)
        if action == 'create':
            value = open_session(manager, arguments.get('title', '新会话'),
                                 workspace_path=arguments.get('workspace_path'),
                                 execution_environment=arguments.get('execution_environment'))
            self._selected = value.id
            self._selected_flow = None
            if 'permission_policy' in arguments:
                manager.set_permission_policy(value, arguments['permission_policy'])
            return {'session_id': value.id}
        if action.startswith('flow_'):
            flow_id = arguments.get('flow_id', self._selected_flow)
            if action == 'flow_create':
                flow = self._flows.create(**{key: val for key, val in arguments.items()
                    if key in {'title', 'workspace_path', 'handoff', 'execution_environment', 'permission_policy', 'model_profile'}})
                self._selected_flow = flow['id']
                await asyncio.to_thread(self._audit, 'flow_created', flow_id=flow['id'])
                return flow
            if action == 'flow_select':
                flow = self._flows.get(flow_id)
                self._selected_flow = flow_id
                return flow
            if action == 'flow_save':
                result = self._flows.save(arguments['definition'])
            elif action == 'flow_run':
                result = self._flows.start(flow_id, arguments.get('definition'))
            elif action == 'flow_cancel':
                self._flows.cancel(flow_id)
                result = self._flows.get(flow_id)
            elif action == 'flow_delete':
                self._flows.delete(flow_id)
                if self._selected_flow == flow_id:
                    self._selected_flow = None
                result = {'flow_id': flow_id}
            else:
                raise ValueError('未知 Flow 操作。')
            await asyncio.to_thread(self._audit, action, flow_id=result.get('id', flow_id))
            return result
        if action.startswith('model_'):
            registry = manager.model_registry
            if len(arguments.get('api_key', '')) >= 8:
                self._secrets.add(arguments['api_key'])
            model_id = arguments.pop('model_id', None)
            arguments.pop('session_id', None)
            if action in {'model_edit', 'model_delete'}:
                for flow in self._flows.list():
                    profiles = {flow.get('model_profile'), *(node.get('model_profile') for node in flow['nodes'])}
                    if model_id in profiles:
                        if action == 'model_delete':
                            raise ValueError('Flow 仍在使用该模型，请先修改流程或节点的模型设置。')
                        if flow['id'] in self._flows.tasks and not self._flows.tasks[flow['id']].done():
                            raise ValueError('Flow 运行期间不能修改其模型连接。')
                for session in manager.sessions.values():
                    if session.record.get('model_profile') == model_id:
                        self._idle(session)
                        if action == 'model_delete':
                            raise ValueError('会话仍在使用该模型，请先切换这些会话的模型。')
            if action == 'model_add':
                result = await asyncio.to_thread(registry.add, **arguments)
                await asyncio.to_thread(self._audit, 'runtime_model_added', model_profile=result['id'])
                return result
            if action == 'model_edit':
                result = await asyncio.to_thread(registry.update, model_id, **arguments)
                await asyncio.to_thread(self._audit, 'runtime_model_updated', model_profile=model_id)
                return result
            if action == 'model_delete':
                await asyncio.to_thread(registry.delete, model_id)
                await asyncio.to_thread(self._audit, 'runtime_model_deleted', model_profile=model_id)
                return {}
            raise ValueError('未知模型操作。')
        if value is None:
            raise ValueError('会话不存在，请重新选择。')
        if action == 'select':
            await self._materialize(value.id)
            self._selected = value.id
            self._selected_flow = None
        elif action == 'older':
            self._page_sizes[value.id] = self._page_sizes.get(value.id, 60) + 60
        elif action == 'send':
            await self._materialize(value.id)
            manager.submit(value, arguments['text'])
            return {'session_id': value.id}
        elif action == 'cancel':
            manager.cancel(value)
        elif action == 'decide':
            if not value.confirmation or value.confirmation['id'] != arguments['confirmation_id']:
                raise ValueError('确认已过期或不属于当前操作。')
            if not manager.decide(value, bool(arguments['allowed'])):
                raise ValueError('确认已结束。')
        elif action == 'delete':
            manager.assert_project_available(value)
            if value.confirmation:
                raise ValueError('请先处理待确认操作。')
            await manager.delete_session(value, cascade=True)
            if self._selected not in manager.sessions:
                self._selected = next(iter(manager.sessions), None)
                await self._materialize(self._selected)
        elif action in {'files', 'file'}:
            from .files import preview
            path = arguments.get('path', '.')
            root = await asyncio.to_thread(manager.workspace, value)
            identity = manager.workspaces.identity(value)
            result = await asyncio.to_thread(preview, root, path, directory=action == 'files', identity=identity)
            await asyncio.to_thread(self._audit, 'desktop_directory_read' if action == 'files' else 'desktop_file_read',
                                    session_id=value.id, path=path)
            return {'session_id': value.id, **result}
        elif action == 'settings':
            self._idle(value)
            manager.assert_project_available(value)
            if manager.workspaces.identity(value) in manager.project_reservations:
                raise ValueError('Flow 运行期间不能修改节点会话设置。')
            await self._materialize(value.id)
            # 在任何 setter 保存前完成整份参数校验，失败不能留下部分权限变更。
            title = arguments.get('title', value.record['title'])
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
                raise ValueError('会话名称应为 1 至 120 字。')
            if 'model_profile' in arguments and not manager.model_registry.contains(arguments['model_profile']):
                raise ValueError('未知模型 profile')
            if 'permission_policy' in arguments:
                from agent_core.core.permissions import ACCEPTED_PERMISSION_POLICIES
                if arguments['permission_policy'] is not None and arguments['permission_policy'] not in ACCEPTED_PERMISSION_POLICIES:
                    raise ValueError('权限策略必须为 full_access/smart/readonly')
            if 'workspace_path' in arguments or 'execution_environment' in arguments:
                if value.record.get('delegated_from') or not manager.workspaces.can_change(value):
                    raise PermissionError('请先结束该项目的 Agent、工具与后台命令，再修改根会话工作区或命令环境。')
            if 'workspace_path' in arguments:
                await asyncio.to_thread(manager.workspaces.validate_path, arguments['workspace_path'])
            if 'execution_environment' in arguments:
                environment = arguments['execution_environment'] or config.COMMAND_EXECUTION_ENVIRONMENT
                if environment not in {'sandbox', 'local'}:
                    raise ValueError('执行环境必须为 sandbox/local')
                if environment == 'local' and sys.platform != 'linux':
                    raise ValueError('本机命令执行目前仅支持 Linux/WSL')
            # 校验目录时已让出事件循环；重新检查根项目状态后连续应用。
            self._idle(value)
            if ('workspace_path' in arguments or 'execution_environment' in arguments) and not manager.workspaces.can_change(value):
                raise ValueError('项目仍有运行中的任务或工具，请稍后保存设置。')
            if 'workspace_path' in arguments:
                manager.set_workspace_path(value, arguments['workspace_path'])
            if 'execution_environment' in arguments:
                manager.set_execution_environment(value, arguments['execution_environment'])
            if 'permission_policy' in arguments:
                manager.set_permission_policy(value, arguments['permission_policy'])
            if 'model_profile' in arguments:
                manager.set_model_profile(value, arguments['model_profile'])
            if 'title' in arguments:
                value.record['title'] = title.strip()
                manager.save(value)
            await manager.flush()
        else:
            raise ValueError('未知桌面操作。')
        return {'session_id': value.id}
