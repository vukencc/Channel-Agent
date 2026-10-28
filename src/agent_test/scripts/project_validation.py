"""真实模型与 WebUI 的隔离工程实验；只写系统临时目录，不伪造模型结果。"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
import time


PROJECT_TASK = """请在当前工作区完整实现一个可运行的离线项目 WorkBoard，用 Python 标准库，禁止安装依赖或联网。交付实际项目文件、测试、README，不要只返回设计或代码片段。
面向团队任务管理，要求分离存储、业务、验证、CLI、HTTP、导入导出模块。项目入口 python3 -m workboard，CLI 输出 JSON，错误写 stderr 并返回非零状态。
固定公共接口：from workboard import Board；Board(db_path) 使用 SQLite，支持 close()，并提供：
create_project(name)->含id,name字典；create_task(project_id,title,priority='normal',depends_on=None)->任务字典；update_task(task_id,**changes)->任务字典；get_task(task_id)->任务字典；list_tasks(project_id,status=None,limit=50,offset=0)->字典列表；add_comment(task_id,text)->含id,text字典；comments(task_id)->列表；add_label(task_id,label)；search(project_id,query)->列表；history(task_id)->操作列表；summary(project_id)->状态计数字典；export_project(project_id)->含project,tasks字典；import_project(payload)->新项目字典。
状态 todo/doing/done，优先级 low/normal/high；空项目名/标题、错误状态、错误优先级、无效外键和负分页参数须 ValueError；任务 id/ project_id 固定为整数。标题带单引号必须安全保存，可按字面搜索；SQL禁止拼接用户内容。
依赖须属于同项目，不允许自引用或循环；未完成前置任务时禁止把依赖任务标 done；更新title/priority/dependencies/status须原子提交并记历史，失败不得部分改动。
评论和标签支持持久化；标签去重；任务字典包含 labels、depends_on；删除项目功能可选，不许删除工作区之外的数据。
导出后可以导入为新项目，必须重建 task id 和依赖引用；损坏或循环导入必须回滚，保留既有数据库。重开 Board 必须恢复任务/评论/标签。
CLI至少实现 init、project-add、task-add、task-list、task-update、summary、export、import，并接受 --db；README写出可直接运行的示例。
HTTP使用 http.server，提供 /health、GET /projects/<id>/tasks、POST /projects、POST /projects/<id>/tasks 和错误JSON；模块设计支持单元测试，无需在隔离命令里联网测试。
写至少30项unittest，覆盖正常路径、依赖/事务/导入失败、安全输入、持久化、分页、CLI，实际执行 python3 -m unittest discover -s tests -v 并修复失败。不要声称未实际通过的验收。
你拥有一个新建的隔离工作区，可直接在这里创建和修改项目；不要写主机绝对路径。最后说明实际验证命令、结果和未完成内容。
"""


def isolate_experiment(settings, root):
    """维护、默认工具及知识缓存也只能指向实验目录。"""
    settings.AUDIT_LOG = root / 'audit.log'
    settings.RAG_CACHE_DIR = root / 'rag-cache'
    settings.AGENT_STATE_DIR = root / 'state'
    settings.SANDBOX_DIR = root / 'work'
    settings.DOC_DIR = root / 'knowledge'
    settings.TOOL_ROOTS = {}
    settings.RAG_SOURCES = {}
    for name in ('AUDIT_LOG', 'RAG_CACHE_DIR', 'AGENT_STATE_DIR', 'SANDBOX_DIR', 'DOC_DIR'):
        if getattr(settings, name).is_symlink():
            raise ValueError('实验路径不能是符号链接')
    settings.DOC_DIR.mkdir(exist_ok=True)


def write_observation(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.observation-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8977)
    parser.add_argument('--max-seconds', type=int, default=900)
    parser.add_argument('--resume', action='store_true', help='仅恢复本脚本创建且已停止的临时实验')
    args = parser.parse_args()
    root = args.root.resolve()
    if (not root.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or root.name.find('agent-project-validation-') != 0 or root.is_symlink()):
        parser.error('实验目录须在系统临时目录下，以 agent-project-validation- 开头且不能是链接')
    if args.resume:
        task = root / 'task.txt'
        if (not root.is_dir() or task.is_symlink() or not task.is_file()
                or task.stat().st_size > 65536 or task.read_text(encoding='utf-8') != PROJECT_TASK
                or any(path.is_symlink() or not path.is_dir() for path in (root / 'state', root / 'work'))):
            parser.error('恢复目录不是本脚本创建的有效隔离实验')
    else:
        if root.exists():
            parser.error('请使用尚不存在的新实验目录，避免覆盖或复用既有状态')
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    from agent_core import config
    isolate_experiment(config, root)
    from agent_core.core.storage import SessionStore
    from agent_core.core.llm import call_model, close_clients
    from agent_frontend.web.app import create_app
    import uvicorn

    config.validate_runtime_config()
    if not args.resume:
        (root / 'task.txt').write_text(PROJECT_TASK, encoding='utf-8')
    events = []
    store = SessionStore(root / 'state', root / 'work')
    app = create_app(store=store, model=call_model)
    original = app.router.lifespan_context

    class ObservedSupervisor:
        def __init__(self, delegate):
            self.delegate, self.settings = delegate, delegate.settings

        async def invoke(self, kind, operation):
            started = time.monotonic()
            event = {'kind': kind, 'started': started}
            events.append(event)
            try:
                result = await operation()
                event['status'] = 'success'
                return result
            except BaseException as exc:
                event['status'] = type(exc).__name__
                raise
            finally:
                event['elapsed_s'] = round(time.monotonic() - started, 3)

        async def inspect(self, snapshot):
            return await self.invoke('inspect', lambda: self.delegate.inspect(snapshot))

        async def summarize(self, previous_summary, messages, *, kind='session'):
            return await self.invoke(kind, lambda: self.delegate.summarize(previous_summary, messages, kind=kind))

        async def close(self):
            await self.delegate.close()

    def collect(manager):
        sessions = []
        for session in manager.sessions.values():
            record = session.record
            calls = [call['function']['name'] for message in record['messages']
                     for call in message.get('tool_calls', [])]
            sessions.append({'id': session.id, 'status': record['status'], 'phase': session.phase,
                'busy': session.busy, 'messages': len(record['messages']), 'tool_names': calls,
                'last_run': record.get('last_run', {}), 'plan': manager.task_plans.peek(session)
                if manager.task_plans else None})
        return {'version': 1, 'sessions': sessions, 'supervisor_events': events}

    async def monitor(manager):
        started = time.monotonic()
        while True:
            payload = collect(manager)
            await asyncio.to_thread(write_observation, root / 'observation.json', payload)
            if time.monotonic() - started > args.max_seconds:
                for session in manager.sessions.values():
                    if session.busy:
                        manager.cancel(session)
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(current):
        try:
            async with original(current):
                manager = current.state.manager
                if manager.history_context:
                    manager.history_context.supervisor = ObservedSupervisor(manager.history_context.supervisor)
                worker = asyncio.create_task(monitor(manager))
                try:
                    yield
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    for session in manager.sessions.values():
                        manager.cancel(session)
                    await asyncio.gather(*(s.task for s in manager.sessions.values() if s.task), return_exceptions=True)
                    write_observation(root / 'observation.json', collect(manager))
        finally:
            store.close()
            await close_clients()

    app.router.lifespan_context = lifespan
    print(json.dumps({'url': f'http://127.0.0.1:{args.port}', 'root': str(root),
        'task_plans': config.ENABLE_TASK_PLANS, 'structured': config.ENABLE_STRUCTURED_CONTEXT}), flush=True)
    try:
        uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_level='warning',
                    timeout_graceful_shutdown=5)
    finally:
        store.close()


if __name__ == '__main__':
    main()
