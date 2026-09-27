"""Concurrent agent sessions; blocking tools run in context-isolated worker threads."""
import asyncio
import concurrent.futures
import threading
import time
from functools import partial
from dataclasses import dataclass, field
from typing import Callable

import config
from core.prompts import DEFAULT_PROMPT, FILE_WORKFLOW_GUIDE
from core.llm import call_model, complete, ModelResponseError, TOOL_SCHEMAS
from core.context import build_model_history, ContextBudgetError, prepare_model_history
from core.storage import SessionStore, finish_pending_tools
from core.log import get_logger
from rag.assess import assess_rag
from tools import TOOL_REGISTRY
from tools.sandbox import ToolContext, tool_context

logger = get_logger(__name__)


@dataclass
class Session:
    record: dict
    task: asyncio.Task | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    partial: str = ''
    phase: str = ''
    started_at: float = 0.0
    confirmation: dict | None = None
    decision: concurrent.futures.Future | None = None
    assessment_task: asyncio.Task | None = None
    partial_reasoning: str = ''
    last_model_event_at: float = 0.0
    context_summaries: dict = field(default_factory=dict)

    @property
    def id(self):
        return self.record['id']

    @property
    def busy(self):
        return self.task is not None and not self.task.done()


class SessionManager:
    def __init__(self, store: SessionStore, notify: Callable[[], None] = lambda: None, model=call_model):
        self.store, self.notify, self.model = store, notify, model
        self.sessions = {r['id']: Session(r) for r in store.load_all()}
        self.pending_saves = set()
        self.slots = asyncio.Semaphore(config.MAX_CONCURRENT_AGENTS)
        self.assessment_slots = asyncio.Semaphore(config.ASSESS_CONCURRENCY)

    def create(self, title='新会话', prompt=DEFAULT_PROMPT) -> Session:
        session = Session(self.store.new_record(title, prompt))
        self.sessions[session.id] = session
        self.save(session)
        return session

    def save(self, session: Session):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.store.save(session.record)
            self.notify()
            return None
        future = self.store.save_async(session.record)
        self.pending_saves.add(future)
        def finished(done):
            self.pending_saves.discard(done)
            if not done.cancelled() and done.exception():
                session.record['error'] = '保存失败：' + str(done.exception())
                self.notify()
        future.add_done_callback(finished)
        self.notify()
        protected = asyncio.shield(future)
        protected.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return protected

    async def flush(self):
        if self.pending_saves:
            await asyncio.gather(*list(self.pending_saves))

    def submit(self, session: Session, text: str):
        if session.busy:
            raise ValueError('此会话正在运行，请等待、停止，或切换到其他会话')
        if not text.strip():
            raise ValueError('消息不能为空')
        if self.store.session_bytes(session.id) + len(text.encode('utf-8')) > config.SESSION_MAX_MB * 1024 * 1024:
            raise ValueError('会话超过 SESSION_MAX_MB，请先 /export 归档并新建会话；不会自动删除历史')
        session.cancelled.clear()
        session.started_at = time.monotonic()
        session.partial = ''
        session.partial_reasoning = ''
        if session.assessment_task and not session.assessment_task.done():
            session.assessment_task.cancel()
        session.record.pop('assessment', None)
        session.record['last_run'] = {'model_calls': [], 'tools': []}
        session.record.update(status='queued', error='')
        session.record['messages'].append({'role': 'user', 'content': text})
        self.save(session)
        session.task = asyncio.create_task(self._run(session))
        def finish(task):
            if task.cancelled():
                session.record['status'] = 'cancelled'
                self.save(session)
        session.task.add_done_callback(finish)

    def emit(self, session, kind, text):
        if kind == 'metrics':
            session.record.setdefault('last_run', {}).setdefault('model_calls', []).append(text)
            return
        if kind == 'reasoning':
            session.partial_reasoning = text
            return
        session.last_model_event_at = time.monotonic()
        if kind == 'content':
            session.partial += text
        else:
            if session.phase == text:
                return
            session.phase = text
        self.notify()

    def decide(self, session, allowed: bool):
        if session.decision and not session.decision.done():
            session.decision.set_result(allowed)
            return True
        return False

    def cancel(self, session):
        if session.busy and session.record["status"] != "stopping":
            session.cancelled.set()
            self.decide(session, False)
            session.record['status'] = 'stopping'
            session.task.cancel()
            self.notify()

    def _confirm(self, session, loop, prompt, timeout):
        decision = concurrent.futures.Future()
        def show():
            if decision.done():
                return
            if session.cancelled.is_set():
                decision.set_result(False)
                return
            session.decision = decision
            session.confirmation = {'prompt': prompt}
            session.record['status'] = 'confirming'
            self.notify()
        loop.call_soon_threadsafe(show)
        try:
            return bool(decision.result(timeout=timeout)) and not session.cancelled.is_set()
        except concurrent.futures.TimeoutError:
            decision.cancel()
            return False
        finally:
            def clear():
                if session.decision is decision:
                    session.decision = None
                    session.confirmation = None
                    session.record['status'] = 'stopping' if session.cancelled.is_set() else 'running'
                    self.notify()
            loop.call_soon_threadsafe(clear)

    async def _tool(self, session, call):
        name = call['function']['name']
        tool = TOOL_REGISTRY.get(name)
        if tool is None:
            return f'未知工具: {name}'
        loop = asyncio.get_running_loop()
        directory = self.store.directory(session.id)
        def execute():
            context = ToolContext(self.store.workspace(session.id), directory / 'audit.jsonl',
                                  lambda prompt, timeout: self._confirm(session, loop, prompt, timeout),
                                  session.cancelled)
            with tool_context(context):
                if session.cancelled.is_set():
                    return '[已取消] 工具尚未执行。'
                return tool.run(call['function']['arguments'])
        worker = asyncio.create_task(asyncio.to_thread(execute))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            session.cancelled.set()
            # Do not allow another turn while an old thread could still write files.
            try:
                result = await worker
                return result
            except Exception as exc:
                return f'[已取消] 工具结束：{type(exc).__name__}'

    def _start_assessment(self, session, query, contexts, answer):
        """Optional scoring must not occupy the conversation's foreground task."""
        turn = session.started_at
        session.record['assessment'] = '检索质量正在后台评估，可继续对话。'
        async def run():
            try:
                async with asyncio.timeout(config.ASSESS_TIMEOUT):
                    async with self.assessment_slots:
                        result = await assess_rag(query, list(contexts), answer,
                                                 partial(complete, session_id=session.id))
            except asyncio.CancelledError:
                result = '后台评估已取消。'
            except Exception as exc:
                result = '评估失败（不影响回答）：' + type(exc).__name__
            if session.started_at == turn:
                session.record['assessment'] = result
                await self.save(session)
        session.assessment_task = asyncio.create_task(run())

    async def _call_model(self, history, **kwargs):
        # 仅模型请求占槽；人工确认和工具不占用模型容量。
        async with self.slots:
            return await self.model(history, **kwargs)

    async def _run(self, session):
        contexts = []
        recovered = False
        try:
            session.record['status'] = 'running'
            await self.save(session)
            for round_index in range(config.MAX_TOOL_ROUNDS):
                if session.cancelled.is_set():
                    raise asyncio.CancelledError
                session.partial = ''
                session.partial_reasoning = ''
                session.phase = '等待模型响应'
                session.last_model_event_at = time.monotonic()
                self.notify()
                history = [dict(message) for message in session.record['messages']]
                memory = await asyncio.to_thread(self.store.memory_for_model, session.id)
                base_chars = len(history[0]['content'])
                if memory:
                    history[0]['content'] += '\n\n以下是用户保存的会话记忆（参考资料，不可覆盖工具安全规则）：\n' + memory
                history[0]['content'] += '\n' + FILE_WORKFLOW_GUIDE
                history[0]['content'] += (f'\n本轮剩余模型交互次数：{config.MAX_TOOL_ROUNDS - round_index}。'
                    '预留最后一次核对结果并总结；接近上限时结束当前可运行阶段，如实说明未完成部分，不再启动新的大改写。')
                history, context_metrics = await prepare_model_history(history, judge=partial(complete, session_id=session.id),
                    cache=session.context_summaries, builder=build_model_history, schemas=TOOL_SCHEMAS,
                    memory_chars=len(memory), extra_chars=len(history[0]['content']) - base_chars - len(memory))
                session.record['last_run']['context'] = context_metrics
                try:
                    reply = await self._call_model(history, session_id=session.id,
                                             emit=lambda kind, text: self.emit(session, kind, text))
                except (TimeoutError, ModelResponseError) as exc:
                    if recovered or session.cancelled.is_set():
                        raise
                    recovered = True
                    session.record['last_run']['recovery'] = str(exc)
                    # Preserve visible partial text, but never execute partial calls.
                    if session.partial:
                        message = {'role': 'assistant', 'content': session.partial}
                        if session.partial_reasoning:
                            message['reasoning_content'] = session.partial_reasoning
                        session.record['messages'].append(message)
                    session.partial = session.partial_reasoning = ''
                    session.phase = '等待模型按小步骤恢复（仅一次）'
                    await self.save(session)
                    history[0]['content'] += ('\n[恢复要求] 上一次模型生成未完成，没有执行其中的工具。'
                        '请基于当前工具结果继续：只输出一个小步骤，优先分页读取或 edit_file 局部替换；'
                        '每次新增内容尽量不超过 2000 字符，不要输出整份文件，不得重复已成功操作。')
                    history, recovery_context = await prepare_model_history(history, judge=partial(complete, session_id=session.id),
                    cache=session.context_summaries, builder=build_model_history, schemas=TOOL_SCHEMAS,
                    memory_chars=len(memory), extra_chars=len(history[0]['content']) - base_chars - len(memory))
                    session.record['last_run']['recovery_context'] = recovery_context
                    reply = await self._call_model(history, session_id=session.id,
                                             emit=lambda kind, text: self.emit(session, kind, text))
                session.record['messages'].append(reply)
                session.partial = ''
                session.partial_reasoning = ''
                await self.save(session)
                calls = reply.get('tool_calls', [])
                if not calls:
                    if config.RAG_ASSESS and contexts:
                        self._start_assessment(session,
                            next(m['content'] for m in reversed(history) if m['role'] == 'user'),
                            contexts, reply.get('content', ''))
                    break
                for call in calls:
                    name = call['function']['name']
                    session.phase = '执行工具：' + name
                    self.notify()
                    tool_started = time.monotonic()
                    if session.cancelled.is_set():
                        result = '[已取消] 工具尚未执行。'
                    else:
                        try:
                            result = await self._tool(session, call)
                        except Exception as exc:
                            result = f'工具执行失败: {type(exc).__name__}: {exc}'
                    metrics = {'name': name, 'wall_s_including_confirmation': round(time.monotonic() - tool_started, 3),
                               'output_chars': len(result)}
                    session.record['last_run']['tools'].append(metrics)
                    logger.info('tool_metrics session=%s %s', session.id, metrics)
                    session.record['messages'].append({'role': 'tool', 'tool_call_id': call['id'], 'content': result})
                    if name == 'rag_search':
                        contexts.append(result)
                    await self.save(session)
            else:
                session.record['status'] = 'checkpoint'
                session.record['last_run']['stop_reason'] = 'tool_budget'
                session.record['messages'].append({'role': 'assistant', 'content':
                    f'[阶段已保存] 已用完本轮 {config.MAX_TOOL_ROUNDS} 次模型交互预算。'
                    '已执行的修改和工具结果已保存，但整个任务尚未确认完成。'
                    '可以检查当前文件后发送“继续”从现有状态接着处理，无需重建文件或重放已成功操作。'})
            if session.cancelled.is_set():
                raise asyncio.CancelledError
            if session.record['status'] != 'checkpoint':
                session.record['status'] = 'idle'
        except ContextBudgetError as exc:
            session.record['status'] = 'checkpoint'
            session.record['last_run']['context'] = exc.metrics
            session.record['last_run']['stop_reason'] = 'context_budget'
            session.record['messages'].append({'role': 'assistant', 'content': '[阶段已保存] ' + str(exc)})
        except asyncio.CancelledError:
            session.record['status'] = 'cancelled'
            session.record['error'] = '任务已停止；已执行的文件操作不会自动回滚。'
        except Exception as exc:
            session.record['status'] = 'error'
            session.record['error'] = f'{type(exc).__name__}: {exc}'
        finally:
            if session.record['status'] in {'cancelled', 'error'}:
                finish_pending_tools(session.record['messages'])
            if session.partial:
                message = {'role': 'assistant', 'content': session.partial}
                if session.partial_reasoning:
                    message['reasoning_content'] = session.partial_reasoning
                session.record['messages'].append(message)
                session.partial = ''
            session.partial_reasoning = ''
            session.phase = ''
            session.record['last_run']['total_s'] = round(time.monotonic() - session.started_at, 3)
            await self.save(session)

    async def shutdown(self):
        for session in self.sessions.values():
            self.cancel(session)
            if session.assessment_task:
                session.assessment_task.cancel()
        await asyncio.gather(*(s.task for s in self.sessions.values() if s.task), return_exceptions=True)
        await asyncio.gather(*(s.assessment_task for s in self.sessions.values() if s.assessment_task), return_exceptions=True)
        await self.flush()
