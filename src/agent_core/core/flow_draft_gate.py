"""Flow 出站草稿的独立模型复核，原文与推理均不写入诊断记录。"""
import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Callable

from agent_core.core.budgets import BudgetExceeded
from agent_core.core.context import history_size, request_tokens
from agent_core.core.flow_draft_review import needs_review, parse_review_reply, review_feedback, review_messages
from agent_core.core.flow_review_sources import review_sources
from agent_core.core.instrumentation import tool_outcome
from agent_core.tools import TOOL_REGISTRY
from agent_core.core.llm import ModelConnectionError, ModelResponseError, RuntimeModelRequestError
from agent_core.core.session_limits import limit as session_limit


class FlowDraftGate:
    """仅属当前节点的当前轮。复核与主模型共享连接、交互轮、费用和时限。"""

    def __init__(self, manager, session, reserve_round: Callable[[], bool]):
        self.manager = manager
        self.session = session
        self.reserve_round = reserve_round
        self.blocked = ''
        self.needs_sources = False
        self.pending_tools: set[tuple[str, str]] = set()

    @staticmethod
    def tool_key(name: str, arguments: str) -> tuple[str, str]:
        try:
            values = json.loads(arguments)
        except (TypeError, ValueError, RecursionError):
            values = {}
        identifiers = {key: value for key, value in values.items()
                       if (key in {'id', 'ref'} or key.endswith(('_id', '_ref')))
                       and type(value) in (str, int)} if isinstance(values, dict) else {}
        encoded = json.dumps(identifiers, ensure_ascii=True, sort_keys=True).encode()
        return name, hashlib.sha256(encoded).hexdigest()

    async def execute_tool(self, call: dict, evidence: dict) -> str:
        if self.blocked:
            return '[已拒绝] 草稿尚未提交。' + self.blocked
        name = call['function']['name']
        custom = self.manager._session_custom_registry(self.session).get(name) if name not in TOOL_REGISTRY else None
        if custom is None:
            return await self.manager._tool(self.session, call, evidence=evidence)
        allowed = self.manager._effective_session_tools(self.session)
        if allowed is not None and name not in allowed:
            return await self.manager._tool(self.session, call, evidence=evidence)
        call = copy.deepcopy(call)
        arguments = call['function']['arguments']
        key = self.tool_key(name, arguments)
        definition = copy.deepcopy(custom.definition)
        feedback = await self.check(arguments, stage='tool', force=key in self.pending_tools, call_id=call['id'])
        current = self.manager._session_custom_registry(self.session).get(name)
        if current is None or current.definition != definition:
            feedback = '工具定义已改变，请重新确认本次调用。'
        if feedback:
            self.pending_tools.add(key)
            return '[已拒绝] 草稿尚未提交。' + feedback
        result = await self.manager._tool(self.session, call, evidence=evidence)
        if evidence['source'] == 'executed' and tool_outcome(result) == 'completed':
            self.pending_tools.discard(key)
        return result

    async def check(self, draft: str, *, stage: str, force: bool = False, call_id: str | None = None) -> str | None:
        if self.blocked:
            return self.blocked
        try:
            value = json.loads(draft)
        except (TypeError, ValueError, RecursionError):
            value = draft
        if not isinstance(value, str):
            draft = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        if not force and not needs_review(draft):
            return None
        run = self.session.record['last_run']
        event = {'stage': stage, 'status': 'pending',
                 'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest()}
        if call_id:
            event['tool_call_id'] = call_id
        run.setdefault('draft_reviews', []).append(event)
        char_limit = session_limit('MODEL_INPUT_CHARS')
        sources = review_sources(self.session.record, char_limit=min(24000, char_limit // 2))
        read_paths = {row.get('path') for row in sources if row['kind'] == 'read_file'}
        reported_paths = {row['path'] for row in sources if row['kind'] == 'custom_result' and row.get('path')}
        pending_paths = sorted(reported_paths - read_paths)
        self.needs_sources = bool(pending_paths) or not any(row['kind'] in {'read_file', 'rag_search'} for row in sources)
        event['source_count'] = len(sources)
        event['source_digest'] = hashlib.sha256(json.dumps(sources, ensure_ascii=True,
            separators=(',', ':'), sort_keys=True).encode()).hexdigest()
        if not sources:
            event['status'] = 'missing_source'
            return '尚无本轮可核对的原文。请先读取相关原始资料，或去掉无依据的事实断言，明确标注推断或待核实事项。'
        task = self.session.flow_review_task
        event['task_digest'] = hashlib.sha256(task.encode('utf-8', 'surrogatepass')).hexdigest()
        history = review_messages(draft, sources, node_task=task)
        if history_size(history) > char_limit or request_tokens(history) > session_limit('MODEL_INPUT_TOKENS'):
            event['status'] = 'context_budget'
            return '草稿与来源超过当前复核窗口。请缩短草稿、按单个事项提交，或保留有原文支持的必要表述。'
        if not self.reserve_round():
            event['status'] = 'round_budget'
            self.blocked = '原模型交互轮次已用完，未获来源复核批准。'
            return self.blocked
        previous_phase = self.session.phase
        self.session.phase = '独立核对草稿来源'
        self.manager._observe_phase(self.session, 'flow_draft_review')
        self.manager.notify()

        def emit(kind, data):
            self.session.last_model_event_at = time.monotonic()
            if kind == 'metrics' and isinstance(data, dict):
                self.manager.emit(self.session, kind, {**data, 'purpose': 'flow_draft_review'})

        attempts = event['attempts'] = []
        try:
            for attempt in range(2):
                if self.session.cancelled.is_set():
                    raise asyncio.CancelledError
                if attempt:
                    # 重试只给审查员一条定位提醒，沿用原始草稿、来源、协议和权限。
                    notice = ('上一请求超时，请先核对各事项有争议的出处。已标明待核实的推测和流程路由，'
                              '只需确认标注与原文一致；无须重新完成各事项的业务任务。')
                    concise = [{**history[0], 'content': history[0]['content'] + notice}, history[1]]
                    if history_size(concise) <= char_limit and request_tokens(concise) <= session_limit('MODEL_INPUT_TOKENS'):
                        history = concise
                    if not self.reserve_round():
                        self.blocked = '独立复核超时且模型交互轮次已用完，草稿尚未批准。'
                        event['status'] = 'TimeoutError'
                        return self.blocked
                    run['recovery_attempts'] = run.get('recovery_attempts', 0) + 1
                attempts.append('pending')
                try:
                    answer = await self.manager._call_model(history, disable_tools=True,
                                                            session_id=self.session.id, emit=emit)
                    break
                except TimeoutError:
                    attempts[-1] = 'TimeoutError'
                    if attempt or not session_limit('MODEL_RECOVERY_LIMIT'):
                        raise
            if self.session.cancelled.is_set():
                raise asyncio.CancelledError
            verdict = parse_review_reply(answer)
            feedback = review_feedback(verdict, sources)
            if feedback and verdict and verdict.get('status') == 'need_source':
                self.needs_sources = True
            if feedback and self.needs_sources and pending_paths:
                feedback += ' 可用已有只读工具核对本轮资料工具传入的路径：' + json.dumps(pending_paths[:3], ensure_ascii=False)
            attempts[-1] = event['status'] = 'revise' if feedback else 'passed'
            return feedback
        except asyncio.CancelledError:
            event['status'] = 'cancelled'
            if attempts and attempts[-1] == 'pending':
                attempts[-1] = 'cancelled'
            raise
        except (BudgetExceeded, ModelConnectionError, ModelResponseError, RuntimeModelRequestError, TimeoutError) as exc:
            event['status'] = type(exc).__name__
            if attempts and attempts[-1] == 'pending':
                attempts[-1] = event['status']
            self.blocked = '独立复核因预算、连接或模型响应中止，草稿尚未批准。'
            return self.blocked
        except Exception as exc:
            event['status'] = type(exc).__name__
            if attempts and attempts[-1] == 'pending':
                attempts[-1] = event['status']
            self.blocked = '来源复核未能完成，草稿尚未提交。'
            return self.blocked
        finally:
            self.session.phase = previous_phase
            self.manager.notify()
