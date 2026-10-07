"""由用户明确确认的维护操作；配置决定范围，绝不接受任意删除路径。"""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat

from ai_agent_startup import config
from ai_agent_startup.core.audit_writer import append_audit, close_audit_handles
from ai_agent_startup.core.storage import now, wait_for_io_completion


@contextmanager
def directory_handle(path: Path):
    """逐级打开目录，拒绝符号链接及不支持安全遍历的平台。"""
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd:
        raise PermissionError('当前平台不支持安全清理目录')
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def absolute_path(path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def resolved_path(path) -> Path:
    try:
        return absolute_path(path).resolve()
    except (OSError, RuntimeError) as exc:
        raise PermissionError('保护路径无法确认，拒绝清理') from exc


def validate_cache_root(manager) -> Path:
    root = absolute_path(config.RAG_CACHE_DIR)
    resolved_root = resolved_path(root)
    project = resolved_path(config.PROJECT_ROOT)
    if root.is_relative_to(project) and not root.is_relative_to(project / '.cache'):
        raise PermissionError('项目内只能清理 .cache 下的缓存，不能清理源代码或配置目录')
    protected = [manager.store.root, manager.store.workspace_root, config.DOC_DIR]
    for source in (config.EMBEDDING_LOCAL_PATH, config.RERANK_LOCAL_PATH):
        if source:
            protected.append(source)
    for path in protected:
        path = resolved_path(path)
        if resolved_root.is_relative_to(path) or path.is_relative_to(resolved_root):
            raise PermissionError('缓存目录与用户数据或本地模型目录重叠，拒绝清理')
    for path in (config.PROJECT_ROOT, Path.home(), Path(root.anchor)):
        if resolved_path(path).is_relative_to(resolved_root):
            raise PermissionError('不能将项目、用户或系统根目录作为清理范围')
    audit_path = resolved_path(config.AUDIT_LOG)
    if audit_path.is_relative_to(resolved_root):
        raise PermissionError('缓存目录包含审计日志，拒绝清理')
    return root


def cache_contents(descriptor: int, *, remove=False) -> dict:
    total = {'files': 0, 'bytes': 0}
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                counts = cache_contents(child, remove=remove)
            finally:
                os.close(child)
            if remove:
                os.rmdir(name, dir_fd=descriptor)
        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            counts = {'files': 1, 'bytes': info.st_size}
            if remove:
                os.unlink(name, dir_fd=descriptor)
        else:
            raise PermissionError('缓存目录含特殊文件，拒绝清理')
        for key in total:
            total[key] += counts[key]
    return total


def inspect_cache(manager, *, remove=False) -> dict:
    root = validate_cache_root(manager)
    try:
        with directory_handle(root) as descriptor:
            return cache_contents(descriptor, remove=remove)
    except FileNotFoundError:
        return {'files': 0, 'bytes': 0}
    except OSError as exc:
        raise PermissionError('缓存目录无法安全打开，拒绝清理') from exc


def log_paths(manager) -> list[Path]:
    main = absolute_path(config.AUDIT_LOG)
    for protected in (manager.store.workspace_root, config.DOC_DIR, manager.store.root):
        if resolved_path(main).is_relative_to(resolved_path(protected)):
            raise PermissionError('配置的审计日志位于用户数据目录内，拒绝清理')
    paths = {main, manager.store.root / 'session-lifecycle.jsonl', manager.store.root / 'cli.log'}
    for identifier in manager.sessions:
        paths.add(manager.store.directory(identifier) / 'audit.jsonl')
    trash = manager.store.root / '.trash'
    if trash.is_symlink():
        raise PermissionError('会话回收区不能是符号链接')
    if trash.is_dir():
        for directory in trash.iterdir():
            if not re.fullmatch(r'[a-f0-9]{32}', directory.name):
                continue
            manager.store.directory(directory.name)
            paths.add(directory / 'audit.jsonl')
    if manager.command_jobs is not None:
        for identifier in manager.command_jobs.records:
            paths.add(manager.command_jobs.directory / (identifier + '.log'))
    return sorted(paths)


def inspect_logs(manager, *, clear=False, writable=False) -> dict:
    total = {'files': 0, 'bytes': 0}
    for path in log_paths(manager):
        try:
            with directory_handle(path.parent) as directory:
                # 不在 open 中截断，须先验证类型及硬链接数量。
                flags = (os.O_RDWR if clear or writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
                descriptor = os.open(path.name, flags, dir_fd=directory)
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise PermissionError('日志必须是没有硬链接的普通文件')
                    total['files'] += 1
                    total['bytes'] += info.st_size
                    if clear:
                        os.ftruncate(descriptor, 0)
                        if config.AUDIT_SYNC:
                            os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PermissionError('日志无法安全打开，拒绝清理') from exc
    return total


def maintenance_busy(manager) -> bool:
    if (manager.closing or manager.maintenance or manager.tool_workers or manager.fork_tasks
            or manager.plan_operations or getattr(manager, 'context_operations', ())):
        return True
    try:
        for session in manager.sessions.values():
            if session.confirmation:
                return True
            manager.check_session_deletion(session)
    except ValueError:
        return True
    return False


def maintenance_preview(manager) -> dict:
    result = {'sessions': {'count': len(manager.sessions)}, 'busy': maintenance_busy(manager)}
    for name, inspect in (('cache', inspect_cache), ('logs', inspect_logs)):
        try:
            result[name] = inspect(manager)
        except (PermissionError, ValueError, OSError) as exc:
            result[name] = {'files': 0, 'bytes': 0, 'error': str(exc)}
    return result


async def cleanup(manager, *, cache=False, logs=False, sessions=False) -> dict:
    """调用方负责用户确认；整个执行期间阻止新任务，并保留维护审计。"""
    if not any((cache, logs, sessions)):
        raise ValueError('请选择至少一种清理内容')
    if maintenance_busy(manager):
        raise ValueError('Agent 或工具仍在运行，请停止并等待后清理')
    manager.maintenance = True
    manager.notify()
    selection = {'cache': cache, 'logs': logs, 'sessions': sessions}
    result = {'cache': {'files': 0, 'bytes': 0}, 'logs': {'files': 0, 'bytes': 0},
              'sessions': {'count': 0, 'deleted_ids': []}}
    started = False
    try:
        auxiliary = [task for session in manager.sessions.values()
                     for task in (session.assessment_task, session.memory_task, session.summary_task)
                     if task is not None and not task.done()]
        for task in auxiliary:
            task.cancel()
        await wait_for_io_completion(asyncio.gather(*auxiliary, return_exceptions=True))
        await wait_for_io_completion(asyncio.create_task(manager.flush()))
        loop = asyncio.get_running_loop()
        def file_cleanup():
            nonlocal started
            # 在任何文件改变前检查所有选中范围及归档目标。
            if cache:
                inspect_cache(manager)
            if logs:
                inspect_logs(manager, writable=True)
            if sessions:
                for identifier in manager.sessions:
                    manager.store.check_session_archive(identifier)
            append_audit(config.AUDIT_LOG, json.dumps({'event': 'maintenance_requested',
                'selection': selection, 'created_at': now()}, ensure_ascii=False), sync=config.AUDIT_SYNC)
            started = True
            if cache:
                from ai_agent_startup.rag.cache import clear_runtime_caches
                clear_runtime_caches()
                result['cache'] = inspect_cache(manager, remove=True)
            if logs:
                close_audit_handles()
                result['logs'] = inspect_logs(manager, clear=True)
        worker = loop.run_in_executor(manager.store.writer, file_cleanup)
        await wait_for_io_completion(worker)
        if sessions:
            targets = list(manager.sessions.values())
            await manager.archive_sessions(targets)
            result['sessions'] = {'count': len(targets), 'deleted_ids': [target.id for target in targets]}
        append_audit(config.AUDIT_LOG, json.dumps({'event': 'maintenance_completed',
            'selection': selection, 'result': result, 'created_at': now()}, ensure_ascii=False), sync=config.AUDIT_SYNC)
        return result
    except BaseException as exc:
        if started:
            append_audit(config.AUDIT_LOG, json.dumps({'event': 'maintenance_failed',
                'selection': selection, 'result': result, 'error_type': type(exc).__name__,
                'created_at': now()}, ensure_ascii=False), sync=config.AUDIT_SYNC)
        raise
    finally:
        manager.maintenance = False
        manager.notify()
