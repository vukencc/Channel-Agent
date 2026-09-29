"""单进程持久子代理队列；确认在工具入口，调度与状态修改仅在事件循环。"""
import asyncio
import copy
import json
import math
import re
import time
import uuid

from ai_agent_startup import config
from ai_agent_startup.core.session_limits import effective_limits, validate_limits
from ai_agent_startup.tools import TOOL_REGISTRY
from ai_agent_startup.tools.sandbox import audit


class AgentTasks:
    def __init__(self, manager, *, require_budget_config=True):
        if require_budget_config and not config.ENABLE_SESSION_BUDGETS:
            raise ValueError('ENABLE_AGENT_TASKS 需要 ENABLE_SESSION_BUDGETS')
        self.manager = manager
        self.directory = manager.store.root / 'agent-tasks'
        if self.directory.is_symlink():
            raise ValueError('子代理任务目录不能是符号链接')
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self.records = {}
        self.tasks = {}
        self.closed = False
        self.slots = asyncio.Semaphore(config.AGENT_TASK_CONCURRENCY)
        for path in self.directory.glob('*.json'):
            if path.is_symlink() or not re.fullmatch('[a-f0-9]{32}', path.stem):
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            if record['status'] in {'queued', 'running'}:
                record.update(status='interrupted', error='进程中断，不自动重放；请检查子会话后重新委派。')
                manager.store.atomic_write(path, json.dumps(record, ensure_ascii=False))
            self.records[path.stem] = record

    async def _save(self, identifier):
        body = json.dumps(self.records[identifier], ensure_ascii=False)
        # 调用工具的默认线程池可能已满；持久化不能反向等待该线程池。
        await asyncio.get_running_loop().run_in_executor(self.manager.store.writer,
            self.manager.store.atomic_write, self.directory / (identifier + '.json'), body)

    async def start(self, parent, task, tool_names, max_rounds, delay_seconds=0, *, title=None):
        """内部入口；调用方须先完成委派确认。"""
        if self.closed or self.manager.closing or parent.cancelled.is_set():
            raise ValueError('父会话已取消或任务管理器正在关闭')
        if parent.record.get('delegated_from'):
            raise ValueError('子代理不能递归委派')
        if not isinstance(task, str) or not task.strip() or len(task) > 16000:
            raise ValueError('任务文本必须为 1..16000 字符')
        if not math.isfinite(delay_seconds) or not 0 <= delay_seconds <= 86400:
            raise ValueError('延时必须在 0..86400 秒之间')
        parent_tools = parent.record.get('tool_names', config.MODEL_TOOL_NAMES)
        allowed = set(TOOL_REGISTRY if parent_tools is None else parent_tools)
        forbidden = {'delegate', 'create_session', 'start_command_job'}
        if not isinstance(tool_names, list) or not all(isinstance(name, str) for name in tool_names) or not set(tool_names) <= allowed - forbidden:
            raise ValueError('子代理工具必须为父会话工具子集，且不得委派或启动长命令')
        limits = effective_limits(parent.record)
        if type(max_rounds) is not int or not 1 <= max_rounds <= min(256, limits['MAX_TOOL_ROUNDS']):
            raise ValueError('子代理轮次不得超过父会话有效上限')
        limits['MAX_TOOL_ROUNDS'] = max_rounds
        limits = validate_limits(limits)
        if sum(record['status'] in {'queued', 'running'} for record in self.records.values()) >= config.AGENT_TASK_MAX_ACTIVE:
            raise ValueError('子代理活动队列已满')
        # 在首次 await 前登记，防止并发委派绕过队列容量。
        identifier = uuid.uuid4().hex
        child = self.manager.create(title or '子任务 · ' + task[:24], parent.record['messages'][0]['content'] +
            '\n[子任务] 独立空工作区，只执行所给任务；不可递归委派。'
            f'调用者会话 ID：{parent.id}。可用 send_session_message 向调用者报告进展或请求信息。')
        child.record.update(delegated_from=parent.id, budget_owner_id=parent.record.get('budget_owner_id', parent.id),
            permission_policy=self.manager.permission_override or parent.record.get('permission_policy') or config.TOOL_PERMISSION_POLICY,
            model_profile=parent.record.get('model_profile'), tool_names=list(dict.fromkeys(tool_names)),
            budget_overrides=limits, memory_namespace='session')
        record = {'id': identifier, 'owner': parent.id, 'child_id': child.id, 'status': 'queued',
                  'created_at': time.time(), 'due_at': time.time() + delay_seconds, 'task': task, 'error': '', 'result': ''}
        self.records[identifier] = record
        try:
            await self.manager.save(child)
            await self._save(identifier)
            if self.closed or parent.cancelled.is_set():
                raise ValueError('委派提交期间已取消')
        except BaseException:
            record.update(status='cancelled', error='委派提交未完成，未启动模型')
            await self._save(identifier)
            raise
        self.tasks[identifier] = asyncio.create_task(self._run(identifier))
        self.manager.notify()
        return identifier

    async def _run(self, identifier):
        record = self.records[identifier]
        child = self.manager.sessions[record['child_id']]
        try:
            await asyncio.sleep(max(0, record['due_at'] - time.time()))
            async with self.slots:
                if record['status'] == 'cancelled' or self.closed:
                    return
                record['status'] = 'running'
                await self._save(identifier)
                self.manager.submit(child, record['task'])
                await asyncio.shield(child.task)
                record['status'] = 'completed' if child.record['status'] == 'idle' else child.record['status']
                record['error'] = child.record.get('error', '')
                record['result'] = next((m.get('content', '') for m in reversed(child.record['messages']) if m['role'] == 'assistant'), '')[:config.TOOL_MAX_OUTPUT]
        except asyncio.CancelledError:
            self.manager.cancel(child)
            if child.task:
                await asyncio.gather(child.task, return_exceptions=True)
            record['status'] = 'cancelled'
        except Exception as exc:
            record.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        finally:
            audit('agent_task_finished', task_id=identifier, child_id=child.id, status=record['status'])
            record['finished_at'] = time.time()
            await self._save(identifier)
            self.manager.notify()

    def status(self, owner, identifier):
        record = self.records.get(identifier)
        if record is None or record['owner'] != owner:
            raise PermissionError('任务不存在或不属于当前会话')
        return copy.deepcopy(record)

    def cancel(self, owner, identifier):
        record = self.status(owner, identifier)
        task = self.tasks.get(identifier)
        if task is not None and not task.done():
            self.records[identifier]['status'] = 'cancelled'
            task.cancel()
            child = self.manager.sessions.get(record['child_id'])
            if child:
                self.manager.cancel(child)
            # 未开始执行的 coroutine 不会进入 finally；单独持久化最终取消状态。
            async def persist():
                await asyncio.gather(task, return_exceptions=True)
                await self._save(identifier)
            saved = asyncio.create_task(persist())
            self.manager.pending_saves.add(saved)
            saved.add_done_callback(self.manager.pending_saves.discard)

    def cancel_owner(self, owner):
        for identifier, record in list(self.records.items()):
            if record['owner'] == owner:
                self.cancel(owner, identifier)

    def stop(self):
        self.closed = True
        for identifier, record in list(self.records.items()):
            self.cancel(record['owner'], identifier)

    async def close(self):
        self.stop()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
