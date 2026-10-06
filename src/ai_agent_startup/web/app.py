"""单用户本地 WebUI：本机接口、合并状态流与原有会话安全机制。"""
import asyncio
import concurrent.futures
import copy
import hmac
import json
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_agent_startup import config
from ai_agent_startup.core.llm import call_model, TOOL_SCHEMAS
from ai_agent_startup.core.permissions import PERMISSION_POLICIES
from ai_agent_startup.core.session_service import open_session
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import LazyRecord, SessionStore
from ai_agent_startup.core.tool_settings import filter_schemas
from ai_agent_startup.tools.sandbox import SandboxError, ToolContext, audit, resolve_path, tool_context
from ai_agent_startup.web.files import directory_entries, text_preview


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class NewSession(Arguments):
    title: str = Field(default='新会话', min_length=1, max_length=120)


class Message(Arguments):
    text: str = Field(min_length=1, max_length=200000)


class Settings(Arguments):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    permission_policy: Literal['readonly', 'standard', 'trusted', 'smart', 'full_access'] | None = None
    model_profile: str | None = None
    tool_names: list[str] | None = None


class Decision(Arguments):
    confirmation_id: str = Field(min_length=1, max_length=64)
    allowed: bool = Field(strict=True)


class Consent(Arguments):
    confirm: bool = Field(strict=True)

    @field_validator('confirm')
    @classmethod
    def explicit_consent(cls, value):
        if value is not True:
            raise ValueError('需要明确同意操作')
        return value


class MemoryEdit(Consent):
    content: str = Field(max_length=100000)


class Cleanup(Consent):
    cache: bool = Field(default=False, strict=True)
    logs: bool = Field(default=False, strict=True)
    sessions: bool = Field(default=False, strict=True)


class StateUpdates:
    """合并频繁 token 通知，所有浏览器共享 revision，不丢失唤醒。"""
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.changed = asyncio.Condition()
        self.revision = 0
        self.timer = None
        self.closed = False

    def notify(self):
        if not self.closed:
            self.loop.call_soon_threadsafe(self.schedule)

    def schedule(self):
        if not self.closed and self.timer is None:
            self.timer = self.loop.call_later(.08, self.publish)

    def publish(self):
        self.timer = None
        if not self.closed:
            asyncio.create_task(self.wake())

    async def wake(self):
        async with self.changed:
            self.revision += 1
            self.changed.notify_all()

    async def wait(self, previous):
        async with self.changed:
            await self.changed.wait_for(lambda: self.revision != previous or self.closed)

    async def close(self):
        self.closed = True
        if self.timer:
            self.timer.cancel()
        await self.wake()


def create_app(*, store: SessionStore | None = None, state_dir: Path | None = None,
               token: str | None = None, model=call_model) -> FastAPI:
    if token is not None and not token.strip():
        raise ValueError('WebUI 需要非空访问令牌')
    assets = Path(__file__).with_name('static')

    @asynccontextmanager
    async def lifespan(app):
        app.state.updates = StateUpdates()
        app.state.io = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='web-io')
        current_store = store or await asyncio.to_thread(SessionStore, Path(state_dir or config.AGENT_STATE_DIR))
        app.state.manager = SessionManager(current_store, notify=app.state.updates.notify,
                                          model=model, cascade_deletions=True)
        app.state.locks = {}
        app.state.mutations = asyncio.Lock()
        try:
            yield
        finally:
            await app.state.updates.close()
            await app.state.manager.shutdown()
            await asyncio.to_thread(app.state.io.shutdown, wait=True)
            if store is None:
                await asyncio.to_thread(current_store.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def local_access(request, call_next):
        if request.url.hostname not in {'localhost', '127.0.0.1', '::1'}:
            return JSONResponse({'detail': 'WebUI 仅接受本机 Host'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': '拒绝跨站访问'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '拒绝跨站访问'}, status_code=403)
        if request.url.path.startswith('/api/'):
            authorization = request.headers.get('authorization', '')
            if token is not None and not hmac.compare_digest(authorization.encode(), ('Bearer ' + token).encode()):
                return JSONResponse({'detail': '请使用启动时提供的访问令牌'}, status_code=401)
            if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
                try:
                    length = int(request.headers.get('content-length', '-1'))
                except ValueError:
                    length = -1
                if length < 0 or length > 1048576:
                    return JSONResponse({'detail': '请求需要 Content-Length 且不能超过 1 MiB'}, status_code=413)
            if app.state.manager.maintenance and request.url.path not in {'/api/maintenance', '/api/events'}:
                return JSONResponse({'detail': '正在清理数据，请稍后重试'}, status_code=409)
        snapshot_paths = {'/api/events', '/api/meta', '/api/sessions', '/api/tasks'}
        if request.url.path.startswith('/api/') and (request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}
                                                     or request.url.path not in snapshot_paths):
            # 文件读取、导出和修改共用屏障；清理须等已有读写及审计完成。
            async with app.state.mutations:
                if app.state.manager.maintenance:
                    return JSONResponse({'detail': '正在清理数据，请稍后重试'}, status_code=409)
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(PermissionError)
    async def permission_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=403)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, exc):
        return JSONResponse({'detail': '文件或目录不存在'}, status_code=404)

    async def io(function, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(app.state.io, partial(function, *args, **kwargs))

    def session(identifier):
        try:
            app.state.manager.store.directory(identifier)
        except ValueError:
            raise HTTPException(404, '会话不存在') from None
        value = app.state.manager.sessions.get(identifier)
        if value is None or value.deleting:
            raise HTTPException(404, '会话不存在或正在删除')
        return value

    def lock(identifier):
        return app.state.locks.setdefault(identifier, asyncio.Lock())

    def summary(value):
        record = value.record
        count = (record.get('message_count', 0) if isinstance(record, LazyRecord) and record.loader is not None
                 else len(record['messages']))
        result = {'id': value.id, 'title': record['title'], 'status': record['status'],
                'phase': value.phase, 'busy': value.busy, 'parent_id': record.get('delegated_from'),
                'permission_policy': app.state.manager.permission_override or record.get('permission_policy') or config.TOOL_PERMISSION_POLICY,
                'model_profile': record.get('model_profile'), 'tool_names': record.get('tool_names', config.MODEL_TOOL_NAMES),
                'workspace_mode': record.get('workspace_mode', 'isolated'),
                'workspace_owner_id': record.get('workspace_owner_id', value.id),
                'unread': app.state.manager.communication.pending_count(value),
                'confirmation': copy.deepcopy(value.confirmation),
                'partial': value.partial, 'partial_reasoning': value.partial_reasoning, 'total_messages': count}
        if app.state.manager.task_plans is not None:
            result['plan'] = app.state.manager.task_plans.peek(value)
        return result

    def snapshot():
        return {'sessions': [summary(value) for value in app.state.manager.sessions.values()],
                'runtime': app.state.manager.concurrency_status(), 'revision': app.state.updates.revision}

    async def materialize(value):
        if isinstance(value.record, LazyRecord):
            await io(value.record.materialize)

    def idle(value):
        if value.busy or value.tool_workers:
            raise ValueError('请等待会话及工具空闲后修改')

    def context(value):
        manager = app.state.manager
        return ToolContext(manager.workspace(value), manager.store.directory(value.id) / 'audit.jsonl',
                           lambda *_: False, value.cancelled,
                           permission_policy=value.record.get('permission_policy'), session_id=value.id)

    def ui_audit(value, event, **fields):
        with tool_context(context(value)):
            audit(event, source='web_user', **fields)

    @app.get('/')
    async def index():
        return FileResponse(assets / 'index.html')

    app.mount('/static', StaticFiles(directory=assets, check_dir=False), name='static')

    @app.get('/api/meta')
    async def meta():
        return {'policies': sorted(PERMISSION_POLICIES), 'models': list(config.MODEL_PROFILES),
                'tools': [row['function']['name'] for row in TOOL_SCHEMAS],
                'limits': app.state.manager.concurrency_status()['limits'],
                'load_errors': app.state.manager.store.errors,
                'task_plans_enabled': app.state.manager.task_plans is not None}

    @app.get('/api/sessions')
    async def sessions():
        return snapshot()

    @app.get('/api/maintenance')
    async def maintenance():
        from ai_agent_startup.core.maintenance import maintenance_preview
        return await io(maintenance_preview, app.state.manager)

    @app.post('/api/maintenance/cleanup')
    async def clear_data(arguments: Cleanup):
        from ai_agent_startup.core.maintenance import cleanup
        return await cleanup(app.state.manager, cache=arguments.cache,
                             logs=arguments.logs, sessions=arguments.sessions)

    @app.get('/api/events')
    async def events(request: Request):
        async def stream():
            while not app.state.updates.closed:
                revision = app.state.updates.revision
                yield 'event: state\ndata: ' + json.dumps(snapshot(), ensure_ascii=False) + '\n\n'
                while not app.state.updates.closed:
                    try:
                        await asyncio.wait_for(app.state.updates.wait(revision), timeout=10)
                        break
                    except TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ': heartbeat\n\n'
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/api/sessions', status_code=201)
    async def new_session(arguments: NewSession):
        value = open_session(app.state.manager, title=arguments.title)
        await app.state.manager.flush()
        return summary(value)

    @app.get('/api/sessions/{identifier}/plan')
    async def task_plan(identifier: str, cursor: str | None = None,
                        limit: int = Query(default=32, ge=1, le=256)):
        value = session(identifier)
        service = app.state.manager.task_plans
        if service is None:
            raise PermissionError('任务计划未启用；设置 ENABLE_TASK_PLANS=true 后重启')
        return await service.get(value, cursor=cursor, limit=limit)

    @app.get('/api/sessions/{identifier}')
    async def detail(identifier: str, before: int | None = Query(default=None, ge=0),
                     limit: int = Query(default=60, ge=1, le=200)):
        async with lock(identifier):
            value = session(identifier)
            await materialize(value)
            messages = value.record['messages']
            end = min(len(messages), before if before is not None else len(messages))
            start = max(0, end - limit)
            return {**summary(value), 'messages': copy.deepcopy(messages[start:end]),
                    'total_messages': len(messages), 'next_before': start,
                    'error': value.record.get('error', '')}

    @app.post('/api/sessions/{identifier}/messages', status_code=202)
    async def send(identifier: str, arguments: Message):
        async with lock(identifier):
            value = session(identifier)
            await materialize(value)
            app.state.manager.submit(value, arguments.text)
            return {'accepted': True, 'session_id': value.id}

    @app.post('/api/sessions/{identifier}/stop')
    async def stop(identifier: str):
        value = session(identifier)
        app.state.manager.cancel(value)
        return summary(value)

    @app.post('/api/sessions/{identifier}/confirmation')
    async def decide(identifier: str, arguments: Decision):
        value = session(identifier)
        pending = value.confirmation
        if not pending or pending.get('id') != arguments.confirmation_id or not app.state.manager.decide(value, arguments.allowed):
            raise ValueError('审批已过期或不属于当前请求，请刷新状态')
        return {'allowed': arguments.allowed}

    @app.patch('/api/sessions/{identifier}')
    async def settings(identifier: str, arguments: Settings):
        async with lock(identifier):
            value = session(identifier)
            idle(value)
            await materialize(value)
            changes = arguments.model_fields_set
            if 'tool_names' in changes:
                filter_schemas(TOOL_SCHEMAS, arguments.tool_names)
            if arguments.model_profile and arguments.model_profile not in config.MODEL_PROFILES:
                raise ValueError('未知模型预设')
            if 'title' in changes and arguments.title is None:
                raise ValueError('名称不能为空')
            for key, method in [('permission_policy', app.state.manager.set_permission_policy),
                                ('model_profile', app.state.manager.set_model_profile),
                                ('tool_names', app.state.manager.set_tool_names)]:
                if key in changes:
                    method(value, getattr(arguments, key))
            if 'title' in changes:
                value.record['title'] = arguments.title
                app.state.manager.save(value)
            await app.state.manager.flush()
            return summary(value)

    @app.delete('/api/sessions/{identifier}')
    async def delete(identifier: str, arguments: Consent):
        async with lock(identifier):
            value = session(identifier)
            if value.confirmation:
                raise ValueError('请先处理待确认操作')
            targets = app.state.manager.session_deletion_targets(value, cascade=True)
            moved = await app.state.manager.delete_session(value, cascade=True)
            return {'deleted_id': identifier, 'deleted_ids': [target.id for target in targets], 'archive': str(moved)}

    @app.get('/api/sessions/{identifier}/export')
    async def export(identifier: str, format: Literal['md', 'json'] = 'md'):
        async with lock(identifier):
            value = session(identifier)
            await app.state.manager.flush()
            path = await io(app.state.manager.store.export_saved, value.id, format)
            return FileResponse(path, filename=path.name, media_type='application/json' if format == 'json' else 'text/markdown')

    @app.get('/api/sessions/{identifier}/memory')
    async def memory(identifier: str):
        async with lock(identifier):
            value = session(identifier)
            content = await io(app.state.manager.store.memory, value.id, value.record.get('memory_namespace'))
            return {'content': content}

    @app.put('/api/sessions/{identifier}/memory')
    async def edit_memory(identifier: str, arguments: MemoryEdit):
        async with lock(identifier):
            value = session(identifier)
            idle(value)
            if len(arguments.content) > config.MEMORY_MAX_CHARS:
                raise ValueError('记忆超过 MEMORY_MAX_CHARS')
            def write():
                ui_audit(value, 'memory_replace_confirmed', chars=len(arguments.content))
                store = app.state.manager.store
                with store._memory_lock:
                    path = store.memory_path(value.id, value.record.get('memory_namespace'))
                    store.atomic_write(path, arguments.content)
            await io(write)
            return {'content': arguments.content}

    def file_path(value, path):
        if path.startswith('@'):
            raise PermissionError('文件浏览器只访问当前项目工作区')
        with tool_context(context(value)):
            try:
                return resolve_path(path)
            except SandboxError as exc:
                raise PermissionError(str(exc)) from None

    @app.get('/api/sessions/{identifier}/files')
    async def files(identifier: str, path: str = Query(default='.', max_length=4096)):
        async with lock(identifier):
            value = session(identifier)
            def read():
                directory = file_path(value, path or '.')
                entries = directory_entries(app.state.manager.workspace(value), directory)
                ui_audit(value, 'web_directory_read', path=path, count=len(entries))
                return {'path': '' if path == '.' else path, 'entries': entries}
            return await io(read)

    @app.get('/api/sessions/{identifier}/file')
    async def file(identifier: str, path: str = Query(min_length=1, max_length=4096)):
        async with lock(identifier):
            value = session(identifier)
            def read():
                target = file_path(value, path)
                preview = text_preview(app.state.manager.workspace(value), target)
                ui_audit(value, 'web_file_read', path=path, truncated=preview['truncated'])
                return {'path': path, **preview}
            return await io(read)

    @app.get('/api/tasks')
    async def tasks():
        manager = app.state.manager
        command_records = []
        if manager.command_jobs is not None:
            with manager.command_jobs.lock:
                command_records = copy.deepcopy([row for row in manager.command_jobs.records.values()
                                                  if row.get('owner') in manager.sessions])
        return {'tasks': copy.deepcopy([row for row in manager.agent_tasks.records.values()
                                       if row['owner'] in manager.sessions and row['child_id'] in manager.sessions]) if manager.agent_tasks else [],
                'commands': command_records}

    @app.post('/api/tasks/{identifier}/cancel')
    async def cancel_task(identifier: str):
        manager = app.state.manager
        if manager.agent_tasks is None or identifier not in manager.agent_tasks.records:
            raise HTTPException(404, '子任务不存在')
        manager.agent_tasks.cancel(manager.agent_tasks.records[identifier]['owner'], identifier)
        return {'cancelled': True}

    return app
