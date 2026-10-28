"""独立诊断路由：由宿主注入会话访问与 IO，沿用宿主安全中间件。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse


SNAPSHOT_PATHS = frozenset({'/api/debug'})


@dataclass(frozen=True)
class DebugWebServices:
    """诊断路由只接收已有运行时能力，不创建另一套会话或存储。"""
    manager: Callable[[], Any]
    session: Callable[[str], Any]
    io: Callable[..., Awaitable[Any]]
    materialize: Callable[[Any], Awaitable[None]]
    context_status: Callable[[Any, Any], dict]


def create_router(services: DebugWebServices) -> APIRouter:
    router = APIRouter()
    assets = Path(__file__).parents[1] / 'static'
    session, io = services.session, services.io
    materialize, context_status = services.materialize, services.context_status

    @router.get('/debug')
    async def debug_page():
        if not services.manager().observer.enabled:
            raise HTTPException(404, '需要 DEBUG=true 并重启服务')
        return FileResponse(assets / 'debug.html')

    def debug_scope(identifier: str | None):
        """仅沿当前可信会话元数据确定根和后代，不读取消息或复活已删 trace。"""
        manager = services.manager()
        values = [value for value in manager.sessions.values() if not value.deleting]
        available = {value.id: value for value in values}

        def root_of(value):
            seen = set()
            while value is not None and value.id not in seen:
                seen.add(value.id)
                parent_id = value.record.get('delegated_from')
                if not parent_id:
                    return value.id
                if not isinstance(parent_id, str):
                    return None
                value = available.get(parent_id)
            return None

        roots = {value.id: root_of(value) for value in values}
        if identifier is not None:
            selected = session(identifier)
            root_id = roots.get(selected.id)
            if root_id is None:
                raise HTTPException(404, '会话所属根不存在或父子关系无效')
        else:
            candidates = [value for value in values if roots[value.id] == value.id]
            activity = {}
            for row in manager.observer.snapshot()['timeline']:
                root_id = roots.get(row.get('session_id'))
                if root_id is not None:
                    activity[root_id] = row['seq']
            busy = {roots[value.id] for value in values if value.busy or value.tool_workers}
            selected = max(candidates, key=lambda value: (
                value.id in busy, activity.get(value.id, 0), value.record.get('updated_at', ''), value.id),
                default=None)
            root_id = selected.id if selected is not None else None
        scoped = [value for value in values if root_id is not None and roots[value.id] == root_id]
        scoped.sort(key=lambda value: (value.id != root_id, not value.busy))
        return root_id, scoped

    @router.get('/api/debug')
    async def debug_snapshot(cursor: int | None = Query(default=None, ge=0, le=9007199254740991),
                             generation: str | None = Query(default=None, max_length=64),
                             session_id: str | None = Query(default=None, min_length=1, max_length=128)):
        manager = services.manager()
        if not manager.observer.enabled:
            raise HTTPException(404, '需要 DEBUG=true 并重启服务')
        # 只读小型元数据，不调用 summary() 或 materialize()。
        root_id, values = debug_scope(session_id)
        identifiers = {value.id for value in values}
        visible = values[:manager.observer.capacity]
        return {'scope_session_id': root_id,
                'trace': manager.observer.snapshot(cursor=cursor, generation=generation,
                                                session_ids=identifiers, scope_session_id=root_id),
                'runtime': manager.concurrency_status(identifiers),
                'agents_omitted': max(0, len(values) - len(visible)),
                'agents': [{'id': value.id, 'parent_id': value.record.get('delegated_from'),
                            'status': value.record.get('status'), 'busy': value.busy,
                            'confirmation': value.confirmation is not None,
                            'context_status': context_status(value, manager.history_context)}
                           for value in visible]}

    @router.get('/api/debug/events/{event_seq}/detail')
    async def debug_event_detail(event_seq: int, generation: str | None = Query(default=None, max_length=64),
                                 offset: int = Query(default=0, ge=0),
                                 limit: int = Query(default=6000, ge=1, le=6000),
                                 session_id: str | None = Query(default=None, min_length=1, max_length=128)):
        manager = services.manager()
        if not manager.observer.enabled:
            raise HTTPException(404, '需要 DEBUG=true 并重启服务')
        if not generation:
            raise HTTPException(422, '详情读取必须指定当前时间线 generation')
        root_id, values = debug_scope(session_id)
        trace = manager.observer.snapshot(session_ids={value.id for value in values}, scope_session_id=root_id)
        if generation != trace['generation']:
            raise HTTPException(410, '服务或会话范围已变化，原时间线事件不可用')
        event = next((row for row in trace['timeline'] if row['seq'] == event_seq), None)
        if event is None:
            raise HTTPException(410 if 0 < event_seq < trace['oldest_seq'] else 404,
                                '事件已超出保留窗口或不存在')
        value = session(event.get('session_id', ''))
        if event['kind'] in {'model.final', 'tool.started', 'tool.completed'}:
            await materialize(value)
        from agent_test.web.debug_details import read_debug_detail
        return {**await io(read_debug_detail, manager, value, event, offset, limit), 'scope_session_id': root_id}

    def archive_reader():
        if not services.manager().observer.enabled:
            raise HTTPException(404, '需要 DEBUG=true 并重启服务')
        from agent_test.web.context_archive import ContextArchiveReader
        return ContextArchiveReader(services.manager())

    @router.get('/api/debug/sessions/{identifier}/context')
    async def context_archive(identifier: str, offset: int = Query(default=0, ge=0),
                              limit: int = Query(default=20, ge=1, le=20)):
        reader = archive_reader()
        return await io(reader.listing, session(identifier), offset, limit)

    @router.get('/api/debug/sessions/{identifier}/context/{chunk_id}')
    async def context_archive_chunk(identifier: str, chunk_id: str,
                                    offset: int = Query(default=0, ge=0),
                                    limit: int = Query(default=6000, ge=1, le=6000)):
        reader = archive_reader()
        if len(chunk_id) != 32:
            raise HTTPException(404, '历史块不存在')
        return await io(reader.chunk, session(identifier), chunk_id, offset, limit)

    # 精确路径在宿主 /static 挂载之前注册，保留现有资源 URL。
    def static_response(filename: str):
        async def respond():
            if not services.manager().observer.enabled:
                raise HTTPException(404, '需要 DEBUG=true 并重启服务')
            return FileResponse(assets / filename)
        return respond

    for filename in ('debug.html', 'debug.css', 'debug.js', 'debug-view.js'):
        router.add_api_route('/static/' + filename, static_response(filename),
                             methods=['GET', 'HEAD'], include_in_schema=False)
    return router
