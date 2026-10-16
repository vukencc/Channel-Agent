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

    async def start(self, parent, task, tool_names, max_rounds=0, delay_seconds=0, *, title=None, workspace_mode=None):
        """内部入口；调用方须先完成委派确认。"""
        if self.closed or self.manager.closing or parent.cancelled.is_set() or parent.deleting:
            raise ValueError('父会话已取消或任务管理器正在关闭')
        if parent.record.get('delegated_from'):
            raise ValueError('子代理不能递归委派')
        if not isinstance(task, str) or not task.strip() or len(task) > 16000:
            raise ValueError('任务文本必须为 1..16000 字符')
        if not math.isfinite(delay_seconds) or not 0 <= delay_seconds <= 86400:
            raise ValueError('延时必须在 0..86400 秒之间')
        parent_tools = parent.record.get('tool_names', config.MODEL_TOOL_NAMES)
        allowed = set(TOOL_REGISTRY if parent_tools is None else parent_tools)
        if tool_names is None:
            tool_names = list(TOOL_REGISTRY if parent_tools is None else parent_tools)
        if not isinstance(tool_names, list) or not all(isinstance(name, str) for name in tool_names) or not set(tool_names) <= allowed:
            raise ValueError('子代理工具必须为父会话工具子集')
        endpoint = self.manager.model_registry.resolve(parent.record.get('model_profile'))
        limits = effective_limits(parent.record, endpoint=endpoint)
        parent_rounds = limits['MAX_TOOL_ROUNDS']
        if (type(max_rounds) is not int or not 0 <= max_rounds <= 256
                or parent_rounds > 0 and max_rounds > parent_rounds):
            raise ValueError('子代理轮次不得超过父会话有效上限')
        limits['MAX_TOOL_ROUNDS'] = parent_rounds if max_rounds == 0 else max_rounds
        limits = validate_limits(limits)
        if sum(record['status'] in {'queued', 'running'} for record in self.records.values()) >= config.AGENT_TASK_MAX_ACTIVE:
            raise ValueError('子代理活动队列已满')
        self.manager.workspaces.binding(parent)
        workspace_mode = self.manager.workspaces.validate_mode(
            'shared' if workspace_mode is None else workspace_mode)
        workspace_guide = ('与直接调用者共享项目工作区，操作同一组项目文件；上下文、状态和记忆仍独立。'
            if workspace_mode == 'shared' else '独立工作区；调用者工作区中的文件不会自动复制。')
        communication = ''
        if 'send_session_message' in tool_names:
            communication += '可用 send_session_message 向调用者报告进展或请求信息。'
        if 'read_session_messages' in tool_names:
            communication += '可用 read_session_messages 分页读取自己的信箱。'
        if not communication:
            communication = '当前工具子集未提供信箱通信工具；完成后由任务结果向调用者返回信息。'
        # 在首次 await 前登记，防止并发委派绕过队列容量。
        identifier = uuid.uuid4().hex
        child = self.manager.create(title or '子任务 · ' + task[:24], parent.record['messages'][0]['content'] +
            '\n[子任务] ' + workspace_guide + '只执行所给任务；不可递归委派。'
            '需要文件产物或测试时，使用实际工具调用并检查真实结果；文本代码或工具调用草稿不代表执行。'
            '仅分析任务可以直接回答；执行任务未完成时须说明缺少的产物或验证，不得把准备执行当作完成。'
            f'调用者会话 ID：{parent.id}。实际可用工具：{", ".join(tool_names) or "无"}。' + communication,
            workspace_mode=workspace_mode, parent=parent)
        child.record.update(delegated_from=parent.id, budget_owner_id=parent.record.get('budget_owner_id', parent.id),
            permission_policy=self.manager.permission_override or parent.record.get('permission_policy') or config.TOOL_PERMISSION_POLICY,
            model_profile=parent.record.get('model_profile'), tool_names=list(dict.fromkeys(tool_names)),
            budget_overrides=limits, memory_namespace='session')
        record = {'id': identifier, 'owner': parent.id, 'child_id': child.id, 'status': 'queued',
                  'workspace_mode': workspace_mode, 'workspace_id': child.record['workspace_owner_id'],
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
        if self.manager.debug.enabled:
            self.manager.debug.event('agent.created', session_id=child.id,
                parent_session_id=parent.id, task_id=identifier,
                turn_id=parent.record.get('last_run', {}).get('turn_id'), state='queued')
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
                self.manager.debug.event('agent.started', session_id=child.id,
                    parent_session_id=record['owner'], task_id=identifier,
                    turn_id=child.record.get('last_run', {}).get('turn_id'), state='running')
                await asyncio.shield(child.task)
                record['status'] = 'completed' if child.record['status'] == 'idle' else child.record['status']
                record['error'] = child.record.get('error', '')
                result = next((m.get('content', '') for m in reversed(child.record['messages']) if m['role'] == 'assistant'), '')
                record['result'] = result[:config.TOOL_MAX_OUTPUT]
                record['result_truncated'] = len(result) > config.TOOL_MAX_OUTPUT
        except asyncio.CancelledError:
            self.manager.cancel(child)
            if child.task:
                await asyncio.gather(child.task, return_exceptions=True)
            record['status'] = 'cancelled'
        except Exception as exc:
            record.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        finally:
            if self.manager.debug.enabled:
                self.manager.debug.event('agent.completed', session_id=child.id,
                    parent_session_id=record['owner'], task_id=identifier,
                    turn_id=child.record.get('last_run', {}).get('turn_id'),
                    state='error' if record['status'] == 'failed' else record['status'])
            audit('agent_task_finished', task_id=identifier, child_id=child.id, status=record['status'])
            record['finished_at'] = time.time()
            await self._save(identifier)
            self.manager.notify()

    def status(self, owner, identifier):
        record = self.records.get(identifier)
        if record is None or record['owner'] != owner:
            raise PermissionError('任务不存在或不属于当前会话')
        return copy.deepcopy(record)

    async def wait(self, owner: str, identifier: str, timeout_seconds: float = 10,
                   *, cancelled=None) -> dict:
        """有界等待已有执行任务；等待结束或取消均不取消、重放子任务。"""
        try:
            self.status(owner, identifier)
        except PermissionError:
            audit('agent_task_wait_denied', task_id=identifier)
            raise
        if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 30:
            raise ValueError('等待时限必须在 0..30 秒之间')
        parent = self.manager.sessions.get(owner)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        runner = self.tasks.get(identifier)
        def is_drained():
            record = self.status(owner, identifier)
            child = self.manager.sessions.get(record['child_id'])
            return (record['status'] not in {'queued', 'running'} and (runner is None or runner.done())
                    and (child is None or not (child.busy or child.tool_workers)))

        while True:
            if (parent is None or parent.cancelled.is_set() or parent.deleting
                    or self.closed or self.manager.closing or (cancelled and cancelled())):
                audit('agent_task_wait_cancelled', task_id=identifier)
                raise ValueError('子代理等待已取消')
            remaining = deadline - asyncio.get_running_loop().time()
            if is_drained() or remaining <= 0:
                break
            # runner 结束后仍须等待已超时的只读线程收尾，不能提前宣称 drained。
            if runner is not None and not runner.done():
                await asyncio.wait({runner}, timeout=min(.05, remaining))
            else:
                await asyncio.sleep(min(.05, remaining))
        record = self.status(owner, identifier)
        drained = is_drained()
        result = self._wait_result(record, drained)
        audit('agent_task_wait_completed' if drained else 'agent_task_wait_timeout',
              task_id=identifier, child_id=record['child_id'], status=record['status'], drained=drained)
        return result

    @staticmethod
    def _wait_result(record: dict, drained: bool) -> dict:
        """按序列化后的长度分配文本预算，始终保留完整 JSON 身份与状态。"""
        result = {'task_id': record['id'], 'child_id': record['child_id'],
                  'status': record['status'], 'drained': drained, 'result': '', 'error': '',
                  'result_truncated': bool(record.get('result_truncated')), 'error_truncated': False}
        encoder = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        maximum = config.TOOL_MAX_OUTPUT
        texts = {name: str(record.get(name, '') or '') for name in ('result', 'error')}
        result.update(texts)
        if len(encoder(result)) <= maximum:
            return result
        for name in texts:
            result[name] = ''
            result[name + '_truncated'] = bool(texts[name]) or result[name + '_truncated']
        base_length = len(encoder(result))
        if base_length > maximum:
            raise ValueError('TOOL_MAX_OUTPUT 太小，无法返回完整子任务身份与状态')
        available = maximum - base_length
        nonempty = [name for name, value in texts.items() if value]
        for index, name in enumerate(nonempty):
            # 错误和结果都获得空间；引号、换行和反斜杠按实际 JSON 转义计费。
            allowance = available // (len(nonempty) - index)
            text = texts[name]
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if len(encoder(text[:middle])) - 2 <= allowance:
                    low = middle
                else:
                    high = middle - 1
            result[name] = text[:low]
            result[name + '_truncated'] = low < len(text) or bool(record.get(name + '_truncated'))
            available -= len(encoder(result[name])) - 2
        # true/false 长度差最多两个字符，最终回收该差值而不截断 JSON。
        while len(encoder(result)) > maximum:
            name = max(('result', 'error'), key=lambda key: len(result[key]))
            result[name] = result[name][:-1]
            result[name + '_truncated'] = True
        return result

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
