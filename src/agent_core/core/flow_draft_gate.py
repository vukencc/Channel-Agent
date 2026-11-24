"""Flow 出站草稿的独立模型复核，原文与推理均不写入诊断记录。"""
import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

from agent_core import config
from agent_core.core.budgets import BudgetExceeded
from agent_core.core.custom_tool_results import custom_result_outcome
from agent_core.core.context import history_size, request_tokens
from agent_core.core.flow_draft_review import (
    AmbiguousDraft, assess_review, needs_review, normalised_draft, parse_review_reply, review_messages,
)
from agent_core.core.flow_review_claims import (
    INVALID_REVIEW, RECEIPT_GUIDANCE, _number_targets, action_review_context, claim_assessment, claim_targets, focal_targets,
    prepare_claim_request, shared_targets, with_revision_navigation,
)
from agent_core.core.flow_review_sources import review_sources
from agent_core.core.instrumentation import tool_outcome
from agent_core.tools import TOOL_REGISTRY
from agent_core.core.llm import ModelConnectionError, ModelResponseError, RuntimeModelRequestError
from agent_core.core.model_settings import ModelDispatchStopped
from agent_core.core.session_limits import limit as session_limit


class FlowDraftGate:
    """仅属当前节点的当前轮。复核与主模型共享连接、交互轮、费用和时限。"""

    def __init__(self, manager, session, reserve_round: Callable[[], bool], *, receipt_index=None):
        self.manager = manager
        self.session = session
        self.reserve_round = reserve_round
        self.receipt_index = receipt_index
        self.restored_reads: Callable[[], dict[str, dict]] | None = None
        self.blocked = ''
        self.needs_sources = False
        self.handoff_feedback_valid = False
        self.pending_tools: set[tuple[str, str]] = set()
        self.pending_items: set[str] = set()
        self.passed_items: set[str] = set()
        self._whole_pending = False
        # 仅本 Gate 实例保存有限的上次争议，不写入会话或审计记录。
        self._revisions: dict[object, tuple[str, str, list[dict]]] = {}

    @staticmethod
    def _batch_summary(child: dict, index: int) -> dict:
        return {'batch_index': index, 'status': child['status'],
                **({'verdict_status': child['verdict_status']} if 'verdict_status' in child else {}),
                **({'superseded': True} if child.get('superseded') else {}),
                'target_count': child.get('target_count', 0),
                'invalid_reasons': child.get('invalid_reasons', []),
                'draft_sha256': child['draft_sha256'], 'source_digest': child.get('source_digest'),
                'task_digest': child.get('task_digest'), 'attempts': child.get('attempts', [])}

    def _review_authority(self, observed_run: dict | None = None) -> tuple:
        record = self.session.record
        run = record['last_run'] if observed_run is None else observed_run
        environment = getattr(self.manager, 'execution_environment', None)
        allowed = getattr(self.manager, '_effective_session_tools', None)
        return (id(run), self.session.id, run.get('turn_id'), record.get('flow_id'), record.get('flow_node_id'),
                record.get('workspace_path'), self.session.flow_review_task, record.get('model_profile'),
                record.get('thinking_mode'), record.get('reasoning_effort'), config.FLOW_REVIEW_THINKING_MODE,
                getattr(self.manager, 'permission_override', None) or record.get('permission_policy'),
                environment(self.session) if environment else None,
                copy.deepcopy(allowed(self.session)) if allowed else None)

    def source_rows(self, char_limit: int) -> list[dict]:
        restored = self.restored_reads() if self.restored_reads is not None else None
        return review_sources(self.session.record, char_limit=char_limit, restored_reads=restored)

    def _whole_binding(self, sources: list[dict] | None = None) -> tuple:
        limit = session_limit('MODEL_INPUT_CHARS')
        if sources is None:
            sources = self.source_rows(min(24000, limit // 2))
        receipt = getattr(self, 'receipt_index', None)
        return (*self._review_authority(), copy.deepcopy(sources), getattr(receipt, 'revision', 0),
                copy.deepcopy(receipt.review_facts()) if receipt else None,
                limit, session_limit('MODEL_INPUT_TOKENS'))

    def _binding_guard(self, sources: list[dict] | None = None) -> Callable[[], bool]:
        binding = self._whole_binding(sources)
        context_changed = False

        def unchanged() -> bool:
            nonlocal context_changed
            if not context_changed:
                try:
                    context_changed = binding != self._whole_binding()
                except Exception:
                    context_changed = True
            return not context_changed

        return unchanged

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
        feedback = await self.check(arguments, stage='tool', force=key in self.pending_tools, call_id=call['id'],
                                    action_context=action_review_context(definition), revision_key=key,
                                    revision_definition=definition)
        current = self.manager._session_custom_registry(self.session).get(name)
        if current is None or current.definition != definition:
            feedback = '工具定义已改变，请重新确认本次调用。'
        if feedback:
            self.pending_tools.add(key)
            return '[已拒绝] 草稿尚未提交。' + feedback
        result = await self.manager._tool(self.session, call, evidence=evidence)
        if evidence['source'] == 'executed':
            if custom_result_outcome(result) == 'business_error':
                self.pending_tools.add(key)
            elif tool_outcome(result) == 'completed':
                self.pending_tools.discard(key)
        return result

    async def check(self, draft: str, *, stage: str, force: bool = False, call_id: str | None = None,
                    action_context: dict | None = None, revision_key: object = None,
                    revision_definition: dict | None = None) -> str | None:
        if stage == 'handoff':
            self.handoff_feedback_valid = False
        if self.blocked:
            return self.blocked
        try:
            draft = normalised_draft(draft)
        except AmbiguousDraft:
            self._revisions.clear()
            event = {'stage': stage, 'status': 'ambiguous_draft',
                     'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest()}
            if call_id:
                event['tool_call_id'] = call_id
            self.session.record['last_run'].setdefault('draft_reviews', []).append(event)
            return '草稿中有重复的 JSON 字段。同一对象的每个字段只能有一个值，请消除歧义后重新提交。'
        if stage != 'handoff':
            try:
                if revision_key is not None and (stage, revision_key) in self._revisions:
                    force = True
            except TypeError:
                pass
            return await self._check_one(draft, stage=stage, force=force, call_id=call_id, recheck=force,
                                         action_context=action_context, revision_key=revision_key,
                                         revision_definition=revision_definition)
        if not force and not self.pending_items and not self._whole_pending and not needs_review(draft):
            return None
        from agent_core.core.model_settings import model_dispatch_guard
        run = self.session.record['last_run']
        events = run.setdefault('draft_reviews', [])
        start = len(events)
        char_limit = session_limit('MODEL_INPUT_CHARS')
        sources = self.source_rows(min(24000, char_limit // 2))
        unchanged = self._binding_guard(sources)

        with model_dispatch_guard(unchanged):
            feedback = await self._check_handoff(draft, sources, force, call_id, unchanged)
        if self.session.cancelled.is_set():
            self.handoff_feedback_valid = False
            raise asyncio.CancelledError
        if unchanged():
            return feedback
        self.handoff_feedback_valid = False
        self._whole_pending = True
        self.passed_items.clear()
        self._revisions.clear()
        if len(events) > start:
            events[start]['status'] = 'context_changed'
        return '复核期间交接上下文已改变，草稿尚未批准，请重新核对。'

    async def _check_handoff(self, draft: str, sources: list[dict], force: bool, call_id: str | None,
                             unchanged: Callable[[], bool]) -> str | None:
        from agent_core.core.flow_review_items import review_units
        units = review_units(draft, sources, required_ids=frozenset(self.pending_items)) if sources else None
        # 无法把先前整稿拒绝关联到事项时，保留整稿复审。
        if units is None or self._whole_pending or (force and not self.pending_items):
            return await self._check_whole(draft, sources, force=force or bool(self.pending_items)
                                           or self._whole_pending, call_id=call_id, unchanged=unchanged)
        original_sources = {row.get('id'): row for row in sources}

        def observed(row):
            original = original_sources.get(row.get('id')) if isinstance(row, dict) else None
            return (isinstance(original, dict) and isinstance(row.get('text'), str)
                    and row['text'] in original.get('text', '')
                    and all(original.get(key) == value for key, value in row.items() if key != 'text'))
        ids = [unit.get('item_id') for unit in units]
        if (any(not isinstance(unit.get('item_id'), str) or not isinstance(unit.get('draft'), str)
                or not isinstance(unit.get('sources'), list)
                or not all(observed(row) for row in unit.get('sources', [])) for unit in units)
                or len(ids) != len(set(ids)) or not self.pending_items.issubset(ids)):
            return await self._check_whole(draft, sources, force=True, call_id=call_id, unchanged=unchanged)
        if not units:
            return None
        events = self.session.record['last_run'].setdefault('draft_reviews', [])
        start = len(events)
        recovery = [False]
        summary = []
        feedback = None
        cancelled = False
        authority = self._review_authority()
        budget = (session_limit('MODEL_INPUT_CHARS'), session_limit('MODEL_INPUT_TOKENS'))
        plans = []
        for unit in units:
            kind = unit.get('kind', 'item')
            receipt_revision = getattr(self.receipt_index, 'revision', 0) if kind != 'item' else 0
            key = hashlib.sha256(json.dumps([kind, unit['item_id'], unit['draft'], unit['sources'],
                authority, receipt_revision, budget], ensure_ascii=True, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
            plans.append((unit, key))
        item_count = next((i for i, unit in enumerate(units) if unit.get('kind', 'item') != 'item'), len(units))
        pending_count = sum(key not in self.passed_items or unit['item_id'] in self.pending_items
                            for unit, key in plans[:item_count])
        if config.FLOW_REVIEW_CONCURRENCY == 2 and pending_count > 1:
            summary, feedback, cancelled = await self._check_independent_items(
                plans[:item_count], call_id, unchanged, recovery)
            plans = plans[item_count:]
        for unit, key in plans:
            if cancelled or feedback:
                break
            item_id = unit['item_id']
            kind = unit.get('kind', 'item')
            if key in self.passed_items and item_id not in self.pending_items:
                summary.append(self._unit_summary(unit, {'status': 'reused'}))
                continue
            try:
                feedback = await self._check_one(unit['draft'], stage='handoff', force=True,
                    sources_override=unit['sources'], unit_kind=kind, recovery=recovery,
                    recheck=item_id in self.pending_items, revision_key=(kind, item_id), dispatch_allowed=unchanged)
            except asyncio.CancelledError:
                cancelled = True
            summary.append(self._unit_summary(unit, events.pop()))
            if cancelled:
                break
            if feedback:
                self.pending_items.add(item_id)
                break
            self.pending_items.discard(item_id)
            self.passed_items.add(key)
        status = 'cancelled' if cancelled else 'passed'
        if feedback and not cancelled:
            status = next((row['status'] for row in summary if row['status'] not in {'passed', 'reused'}),
                          'context_changed')
        event = {'stage': 'handoff', 'status': status,
                 'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest(),
                 'unit_count': len(units), 'units': summary,
                 'attempts': [attempt for row in summary for attempt in row.get('attempts', [])]}
        events.insert(start, event)
        if cancelled:
            raise asyncio.CancelledError
        return feedback

    @staticmethod
    async def _run_two_review_slots[Entry, Result](
        work: list[Entry], review: Callable[[Entry], Awaitable[Result]],
        may_dispatch: Callable[[], bool], stopped: list[bool],
    ) -> tuple[list[asyncio.Task[Result]], bool]:
        """滚动填充两个槽，按派发顺序返回任务；取消时等待在途收尾。"""
        async def guarded(entry: Entry) -> Result:
            try:
                return await review(entry)
            except BaseException:
                stopped[0] = True
                raise

        tasks = []
        pending = set()
        cancelled = False
        while pending or (len(tasks) < len(work) and not stopped[0] and not cancelled):
            while len(pending) < 2 and len(tasks) < len(work) and may_dispatch():
                task = asyncio.create_task(guarded(work[len(tasks)]))
                tasks.append(task)
                pending.add(task)
            if not pending:
                break
            try:
                _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                stopped[0] = True
                if not cancelled:
                    for task in pending:
                        if not task.done():
                            task.cancel()
                cancelled = True
        return tasks, cancelled

    @staticmethod
    def _unit_summary(unit: dict, child: dict) -> dict:
        return {'item_sha256': hashlib.sha256(unit['item_id'].encode('utf-8', 'surrogatepass')).hexdigest(),
                'kind': unit.get('kind', 'item'), 'status': child['status'],
                'target_count': child.get('target_count', 0),
                'invalid_reasons': child.get('invalid_reasons', []),
                'draft_sha256': child.get('draft_sha256') or hashlib.sha256(
                    unit['draft'].encode('utf-8', 'surrogatepass')).hexdigest(),
                'attempts': child.get('attempts', []),
                **({'batch_count': child['batch_count'], 'batches': child['batches']} if 'batches' in child else {})}

    async def _check_independent_items(self, plans: list[tuple[dict, str]], call_id: str | None,
                                       unchanged: Callable[[], bool], recovery: list[bool]) -> tuple[list[dict], str | None, bool]:
        from agent_core.core.model_settings import model_dispatch_guard

        original_run = self.session.record['last_run']
        stopped = [False]
        previous_phase = self.session.phase
        self.session.phase = '独立核对草稿来源'
        self.manager._observe_phase(self.session, 'flow_draft_review')
        self.manager.notify()

        def may_dispatch():
            if stopped[0] or self.session.cancelled.is_set():
                return False
            if not unchanged():
                stopped[0] = True
            return not stopped[0]

        async def review(plan):
            unit, key = plan
            item_id = unit['item_id']
            if key in self.passed_items and item_id not in self.pending_items:
                return None, {'status': 'reused'}, None, False
            local_session = copy.copy(self.session)
            local_run = {**original_run, 'draft_reviews': [], 'recovery_attempts': 0}
            local_session.record = {**self.session.record, 'last_run': local_run}
            local_gate = FlowDraftGate(self.manager, local_session, self.reserve_round)
            state_key = ('handoff', ('item', item_id))
            if state_key in self._revisions:
                local_gate._revisions[state_key] = self._revisions[state_key]
            cancelled = False
            feedback = None
            try:
                with model_dispatch_guard(may_dispatch):
                    feedback = await local_gate._check_one(unit['draft'], stage='handoff', force=True, call_id=call_id,
                        sources_override=unit['sources'], unit_kind='item', recovery=recovery,
                        recheck=item_id in self.pending_items, revision_key=('item', item_id), dispatch_allowed=may_dispatch,
                        phase_session=self.session, observed_run=original_run)
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                local_gate.blocked = feedback = '来源复核未能完成，草稿尚未提交。'
                if not local_run['draft_reviews']:
                    local_run['draft_reviews'].append({})
                local_run['draft_reviews'][-1]['status'] = type(exc).__name__
            if feedback or cancelled:
                stopped[0] = True
            original_run['recovery_attempts'] = (
                original_run.get('recovery_attempts', 0) + local_run['recovery_attempts'])
            child = local_run['draft_reviews'][-1] if local_run['draft_reviews'] else {'status': 'cancelled'}
            return local_gate, child, feedback, cancelled

        try:
            tasks, cancelled = await self._run_two_review_slots(plans, review, may_dispatch, stopped)
        finally:
            if (self.session.record.get('last_run') is original_run
                    and self.session.phase == '独立核对草稿来源'):
                self.session.phase = previous_phase
            self.manager.notify()
        cancelled = cancelled or self.session.cancelled.is_set()
        summary = []
        feedback = None
        verdicts = []
        for task, (unit, key) in zip(tasks, plans):
            try:
                local_gate, child, item_feedback, item_cancelled = task.result()
            except asyncio.CancelledError:
                cancelled = True
                summary.append(self._unit_summary(unit, {'status': 'cancelled'}))
                continue
            except Exception as exc:
                self.blocked = '来源复核未能完成，草稿尚未提交。'
                summary.append(self._unit_summary(unit, {'status': type(exc).__name__}))
                continue
            summary.append(self._unit_summary(unit, child))
            cancelled = cancelled or item_cancelled
            if local_gate is None:
                continue
            self.needs_sources = self.needs_sources or local_gate.needs_sources
            self.blocked = self.blocked or local_gate.blocked
            if item_cancelled:
                continue
            item_id = unit['item_id']
            state_key = ('handoff', ('item', item_id))
            self._revisions.pop(state_key, None)
            if item_feedback:
                self.pending_items.add(item_id)
                if state_key in local_gate._revisions and len(self._revisions) < 16:
                    self._revisions[state_key] = local_gate._revisions[state_key]
                feedback = feedback or item_feedback
                if local_gate.handoff_feedback_valid and item_feedback not in verdicts:
                    verdicts.append(item_feedback)
            elif child['status'] == 'passed':
                self.pending_items.discard(item_id)
                self.passed_items.add(key)
        if self.blocked:
            return summary, self.blocked, cancelled
        if not cancelled and verdicts:
            self.handoff_feedback_valid = True
            combined = '\n'.join(verdicts)
            message = [{'role': 'user', 'content': combined}]
            if history_size(message) <= session_limit('MODEL_INPUT_CHARS') and request_tokens(message) <= session_limit('MODEL_INPUT_TOKENS'):
                feedback = combined
            else:
                feedback = verdicts[0]
        if not feedback and len(summary) != len(plans):
            feedback = '来源复核尚未覆盖全部事项，草稿尚未批准。'
        return summary, feedback, cancelled

    async def _check_whole(self, draft: str, sources: list[dict], *, force: bool,
                           call_id: str | None, unchanged: Callable[[], bool]) -> str | None:
        """整稿回退可定位大范围时，逐批核对，但每批仍保留完整上下文。"""
        self._whole_pending = True
        targets = claim_targets(draft)
        # 与原整稿审查使用相同的全局编号，在切成各批后不重新编号。
        selected = targets if force else focal_targets(targets)
        if sources and 4 < len(selected) <= 64:
            feedback = await self._check_batches(
                draft, sources, selected, stage='handoff', call_id=call_id, unchanged=unchanged)
        else:
            feedback = await self._check_one(draft, stage='handoff', force=force, call_id=call_id,
                                             sources_override=sources, recheck=force, dispatch_allowed=unchanged)
        if not feedback:
            self._whole_pending = False
            self.pending_items.clear()
        return feedback

    async def _check_batches(self, draft: str, sources: list[dict], selected: list[dict], *,
                             stage: str, call_id: str | None, unchanged: Callable[[], bool],
                             action_context: dict | None = None, revision_context: dict | None = None,
                             unit_kind: str | None = None, recovery: list[bool] | None = None,
                             revision_key: object = None, phase_session=None,
                             observed_run: dict | None = None) -> str | None:
        # 事项已经由外层调度；在占用的槽内依次发出各批，避免并发嵌套。
        if unit_kind is None and config.FLOW_REVIEW_CONCURRENCY == 2:
            try:
                return await self._check_parallel_batches(draft, sources, selected, stage=stage,
                                                         call_id=call_id, unchanged=unchanged,
                                                         action_context=action_context, revision_context=revision_context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.blocked = '来源复核未能完成，草稿尚未提交。'
                self.session.record['last_run'].setdefault('draft_reviews', []).append({
                    'stage': stage, 'status': type(exc).__name__,
                    'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest(),
                    **({'tool_call_id': call_id} if call_id else {}),
                })
                return self.blocked
        events = self.session.record['last_run'].setdefault('draft_reviews', [])
        start = len(events)
        if recovery is None:
            recovery = [False]
        summary = []
        feedback = None
        cancelled = False
        for offset in range(0, len(selected), 4):
            batch = selected[offset:offset + 4]
            try:
                feedback = await self._check_one(draft, stage=stage, force=True, call_id=call_id,
                    sources_override=sources, recovery=recovery, action_context=action_context,
                    batch_targets=batch, dispatch_allowed=unchanged, revision_context=revision_context,
                    unit_kind=unit_kind, revision_key=revision_key, phase_session=phase_session, observed_run=observed_run)
            except asyncio.CancelledError:
                cancelled = True
            child = events.pop()
            summary.append(self._batch_summary(child, offset // 4 + 1))
            if cancelled or feedback:
                break
        event = {'stage': stage, 'status': summary[-1]['status'] if feedback or cancelled else 'passed',
                 'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest(),
                 'batch_count': (len(selected) + 3) // 4, 'target_count': len(selected), 'batches': summary,
                 'attempts': [attempt for row in summary for attempt in row.get('attempts', [])]}
        if call_id:
            event['tool_call_id'] = call_id
        events.insert(start, event)
        if cancelled:
            raise asyncio.CancelledError
        return feedback

    async def _check_parallel_batches(self, draft: str, sources: list[dict], targets: list[dict], *,
                                      stage: str, call_id: str | None, unchanged: Callable[[], bool],
                                      action_context: dict | None,
                                      revision_context: dict | None) -> str | None:
        """每批隔离状态及审计；只共享轮次、恢复名额和原会话的模型费用。"""
        events = self.session.record['last_run'].setdefault('draft_reviews', [])
        original_run = self.session.record['last_run']
        recovery = [False]
        stopped = [False]
        rejected = [False]
        summary = []
        feedback = None
        has_verdict_feedback = False
        verdict_feedback = []
        cancelled = False
        sources = copy.deepcopy(sources)
        task_snapshot = self.session.flow_review_task
        receipt_index = getattr(self, 'receipt_index', None)
        if stage == 'handoff' and receipt_index is not None:
            facts = copy.deepcopy(receipt_index.review_facts())
            receipt_index = SimpleNamespace(revision=getattr(receipt_index, 'revision', 0),
                                            review_facts=lambda: facts)
        batches = [targets[offset:offset + 4] for offset in range(0, len(targets), 4)]
        previous_phase = self.session.phase
        self.session.phase = '独立核对草稿来源'
        self.manager._observe_phase(self.session, 'flow_draft_review')
        self.manager.notify()

        def may_dispatch():
            if stopped[0] or self.session.cancelled.is_set():
                return False
            if not unchanged():
                stopped[0] = True
            return not stopped[0]

        async def run_batch(batch):
            local_session = copy.copy(self.session)
            local_session.flow_review_task = task_snapshot
            local_session.record = dict(self.session.record)
            local_run = dict(original_run)
            local_run['draft_reviews'] = []
            local_run['recovery_attempts'] = 0
            local_session.record['last_run'] = local_run
            local_gate = FlowDraftGate(self.manager, local_session, self.reserve_round, receipt_index=receipt_index)
            try:
                from agent_core.core.model_settings import model_dispatch_guard
                with model_dispatch_guard(may_dispatch):
                    result = await local_gate._check_one(
                        draft, stage=stage, force=True, call_id=call_id, sources_override=sources,
                        recovery=recovery, action_context=action_context, batch_targets=batch,
                        phase_session=self.session, dispatch_allowed=may_dispatch,
                        observed_run=original_run, revision_context=revision_context)
            except asyncio.CancelledError:
                result = None
                was_cancelled = True
            except Exception as exc:
                local_gate.blocked = '来源复核未能完成，草稿尚未提交。'
                result = local_gate.blocked
                was_cancelled = False
                if not local_run['draft_reviews']:
                    local_run['draft_reviews'].append({
                        'stage': stage,
                        'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest(),
                    })
                local_run['draft_reviews'][-1]['status'] = type(exc).__name__
            else:
                was_cancelled = False
            child = local_run['draft_reviews'][-1]
            if child.get('verdict_status') in {'revise', 'need_source'}:
                rejected[0] = True
            elif (rejected[0] and child.get('status') in
                  {'review_stopped', 'ModelDispatchStopped', 'TimeoutError', 'invalid_verdict'}):
                child['superseded'] = True
            if result or was_cancelled:
                stopped[0] = True
            original_run['recovery_attempts'] = (
                original_run.get('recovery_attempts', 0) + local_run.get('recovery_attempts', 0))
            return local_gate, child, result, was_cancelled

        try:
            tasks, cancelled = await self._run_two_review_slots(batches, run_batch, may_dispatch, stopped)
            for index, task in enumerate(tasks, 1):
                try:
                    result = task.result()
                except BaseException as exc:
                    result = exc
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        cancelled = True
                        status = 'cancelled'
                    else:
                        status = type(result).__name__
                        self.blocked = '来源复核未能完成，草稿尚未提交。'
                        feedback = feedback or self.blocked
                    summary.append(self._batch_summary({
                        'status': status,
                        'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest(),
                    }, index))
                    continue
                local_gate, child, child_feedback, child_cancelled = result
                self.needs_sources = self.needs_sources or local_gate.needs_sources
                if local_gate.blocked and not self.blocked and not child.get('superseded'):
                    self.blocked = local_gate.blocked
                summary.append(self._batch_summary(child, index))
                valid_feedback = 'verdict_status' in child
                if child_feedback and (feedback is None or (valid_feedback and not has_verdict_feedback)):
                    feedback = child_feedback
                if valid_feedback and child_feedback and child_feedback not in verdict_feedback:
                    verdict_feedback.append(child_feedback)
                has_verdict_feedback = has_verdict_feedback or valid_feedback
                cancelled = cancelled or child_cancelled
        finally:
            if (self.session.record.get('last_run') is original_run
                    and self.session.phase == '独立核对草稿来源'):
                self.session.phase = previous_phase
            self.manager.notify()
        cancelled = cancelled or self.session.cancelled.is_set()
        if not feedback and len(summary) != len(batches):
            feedback = '来源复核尚未覆盖全部目标，草稿尚未批准。'
        if not cancelled and verdict_feedback and not self.blocked:
            if stage == 'handoff':
                self.handoff_feedback_valid = True
            if len(verdict_feedback) == 2:
                combined = '\n'.join(verdict_feedback)
                message = [{'role': 'user', 'content': combined}]
                if (history_size(message) <= session_limit('MODEL_INPUT_CHARS')
                        and request_tokens(message) <= session_limit('MODEL_INPUT_TOKENS')):
                    feedback = combined
        event = {'stage': stage, 'status': 'cancelled' if cancelled else
                 next((row['status'] for row in summary if row['status'] != 'passed'),
                      'context_changed' if feedback else 'passed'),
                 'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest(),
                 'batch_count': len(batches), 'target_count': len(targets), 'batches': summary,
                 'attempts': [attempt for row in summary for attempt in row.get('attempts', [])]}
        if call_id:
            event['tool_call_id'] = call_id
        events.append(event)
        if cancelled:
            raise asyncio.CancelledError
        return feedback

    async def _check_one(self, draft: str, *, stage: str, force: bool = False, call_id: str | None = None,
                         sources_override: list[dict] | None = None, unit_kind: str | None = None,
                         recovery: list[bool] | None = None, recheck: bool = False,
                         action_context: dict | None = None, revision_key: object = None,
                         revision_definition: dict | None = None, batch_targets: list[dict] | None = None,
                         phase_session=None, dispatch_allowed: Callable[[], bool] | None = None,
                         observed_run: dict | None = None, revision_context: dict | None = None) -> str | None:
        if self.blocked:
            return self.blocked
        if not force and not needs_review(draft):
            return None
        run = self.session.record['last_run']
        event = {'stage': stage, 'status': 'pending',
                 'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest()}
        if call_id:
            event['tool_call_id'] = call_id
        run.setdefault('draft_reviews', []).append(event)
        char_limit = session_limit('MODEL_INPUT_CHARS')
        sources = (sources_override if sources_override is not None else
                   self.source_rows(min(24000, char_limit // 2)))
        if stage in {'tool', 'preview'} and batch_targets is None:
            from agent_core.core.flow_review_items import tool_review_sources
            sources = tool_review_sources(draft, sources)
        read_paths = {row.get('path') for row in sources if row['kind'] == 'read_file'}
        reported_paths = {row['path'] for row in sources if row['kind'] == 'custom_result' and row.get('path')}
        pending_paths = sorted(reported_paths - read_paths)
        self.needs_sources = bool(pending_paths) or not any(row['kind'] in {'read_file', 'rag_search'} for row in sources)
        event['source_count'] = len(sources)
        event['source_digest'] = hashlib.sha256(json.dumps(sources, ensure_ascii=True,
            separators=(',', ':'), sort_keys=True).encode()).hexdigest()
        state_key = None
        if revision_key is not None:
            try:
                state_key = (stage, revision_key)
                hash(state_key)
            except TypeError:
                state_key = None
        if stage in {'tool', 'preview'} and not isinstance(revision_definition, dict):
            if state_key is not None:
                self._revisions.pop(state_key, None)
            state_key = None
        previous = self._revisions.pop(state_key, None) if state_key is not None else None
        if not sources:
            event['status'] = 'missing_source'
            return '尚无本轮可核对的原文。请先读取相关原始资料，或去掉无依据的事实断言，明确标注推断或待核实事项。'
        task = self.session.flow_review_task
        event['task_digest'] = hashlib.sha256(task.encode('utf-8', 'surrogatepass')).hexdigest()
        receipt_index = getattr(self, 'receipt_index', None)
        receipt_revision = (getattr(receipt_index, 'revision', 0)
                            if stage == 'handoff' and unit_kind != 'item' else 0)
        receipt_budget = (char_limit, session_limit('MODEL_INPUT_TOKENS')) if receipt_revision else None
        authority = self._review_authority(observed_run) if state_key is not None else None
        fingerprint = hashlib.sha256(json.dumps([sources, task, action_context, unit_kind, revision_definition,
            authority, receipt_revision, receipt_budget],
            ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        shared = unit_kind == 'shared_claim'
        if batch_targets is not None:
            targets = batch_targets
        else:
            targets = shared_targets(draft) if shared else claim_targets(draft)
        if batch_targets is None and not shared and not recheck:
            candidates = targets
            targets = focal_targets(candidates)
            event['background_count'] = len(candidates) - len(targets)
        if recheck and previous is not None and previous[0] == fingerprint and targets:
            from agent_core.core.flow_review_revision import revision_navigation, revision_targets

            # 退回意见可能只列出部分错误。旧争议和改动只能补充本次范围。
            revisions = revision_targets(previous[1], previous[2], draft) or []
            spans = {(row['field'], row['start'], row['end']) for row in targets}
            extra = [row for row in revisions if (row['field'], row['start'], row['end']) not in spans]
            targets = _number_targets([*targets, *extra]) if len(targets) + len(extra) <= 64 else []
            revision_context = revision_navigation(previous[1], previous[2], draft, targets, sources)
        if (stage == 'handoff' and batch_targets is None and 4 < len(targets) <= 64
                and unit_kind in {'item', 'shared_claim'}):
            # 目标及修订导航已基于完整事项生成；每批继续使用同一原稿和来源。
            run['draft_reviews'].pop()
            return await self._check_batches(
                draft, sources, targets, stage=stage, call_id=call_id,
                unchanged=dispatch_allowed or self._binding_guard(), action_context=action_context,
                revision_context=revision_context, unit_kind=unit_kind, recovery=recovery,
                revision_key=revision_key, phase_session=phase_session, observed_run=observed_run)
        if stage == 'preview' and batch_targets is None and 4 < len(targets) <= 64:
            from agent_core.core.model_settings import model_dispatch_guard

            events = run['draft_reviews']
            events.pop()
            start = len(events)
            unchanged = self._binding_guard()
            with model_dispatch_guard(unchanged):
                feedback = await self._check_batches(
                    draft, sources, targets, stage=stage, call_id=call_id,
                    unchanged=unchanged, action_context=action_context, revision_context=revision_context)
            if self.session.cancelled.is_set():
                raise asyncio.CancelledError
            if not unchanged():
                events[start]['status'] = 'context_changed'
                return '复核期间草稿上下文已改变，草稿尚未批准，请重新核对。'
            return feedback
        event['target_count'] = len(targets)
        passages = []
        include_receipts = stage == 'handoff' and unit_kind != 'item' and receipt_index is not None
        facts = receipt_index.review_facts() if include_receipts and batch_targets is not None else None
        if targets:
            history, passages = prepare_claim_request(
                draft, sources, targets=targets, node_task=task, per_item=unit_kind == 'item', shared=shared,
                action_context=action_context, char_limit=char_limit, token_limit=session_limit('MODEL_INPUT_TOKENS'),
                whole_batch=batch_targets is not None, receipt_facts=facts)
        else:
            history = review_messages(draft, sources, node_task=task, per_item=unit_kind == 'item')
        event['passage_count'] = len(passages)
        if history_size(history) > char_limit or request_tokens(history) > session_limit('MODEL_INPUT_TOKENS'):
            event['status'] = 'context_budget'
            if facts and batch_targets is not None:
                return '草稿、来源与回执超过当前复核窗口，草稿尚未批准。'
            return '草稿与来源超过当前复核窗口。请缩短草稿、按单个事项提交，或保留有原文支持的必要表述。'
        if include_receipts and batch_targets is None and (facts := receipt_index.review_facts()):
            payload = json.loads(history[1]['content'])
            payload['action_receipt_summary'] = facts
            augmented = [
                {**history[0], 'content': history[0]['content'] + RECEIPT_GUIDANCE},
                {**history[1], 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))},
            ]
            if (history_size(augmented) <= char_limit
                    and request_tokens(augmented) <= session_limit('MODEL_INPUT_TOKENS')):
                history = augmented
        if revision_context:
            fields = {target['field'] for target in targets}
            navigation = {key: [row for row in rows if row['field'] in fields]
                          for key, rows in revision_context.items()}
            history = with_revision_navigation(
                history, targets, navigation, char_limit=char_limit, token_limit=session_limit('MODEL_INPUT_TOKENS'))
        if dispatch_allowed is not None and not dispatch_allowed():
            event['status'] = 'review_stopped'
            return '交接复核已停止，草稿尚未批准。'
        if not self.reserve_round():
            event['status'] = 'round_budget'
            self.blocked = '原模型交互轮次已用完，未获来源复核批准。'
            return self.blocked
        previous_phase = self.session.phase
        if phase_session is None:
            self.session.phase = '独立核对草稿来源'
            self.manager._observe_phase(self.session, 'flow_draft_review')
            self.manager.notify()
        observed_session = phase_session or self.session

        def emit(kind, data):
            if observed_run is not None and observed_session.record.get('last_run') is not observed_run:
                return
            observed_session.last_model_event_at = time.monotonic()
            if kind == 'metrics' and isinstance(data, dict):
                self.manager.emit(observed_session, kind, {**data, 'purpose': 'flow_draft_review'})

        attempts = event['attempts'] = []
        review_options = ({'flow_review_thinking_disabled': True}
                          if config.FLOW_REVIEW_THINKING_MODE == 'disabled' else {})
        try:
            retry_status = None
            for attempt in range(2):
                if self.session.cancelled.is_set():
                    raise asyncio.CancelledError
                if attempt:
                    if dispatch_allowed is not None and not dispatch_allowed():
                        event['status'] = retry_status
                        return '交接复核已停止，草稿尚未批准。'
                    notice = ('上一请求超时，请只核对有争议的出处，保留原文中的限定和否定。'
                              if retry_status == 'TimeoutError' else
                              '上次判定无法核验。请重新独立判断。优先从本次所给编号选择精确目标和证据，'
                              '没有对应编号时逐字引用；资料不足则请求具体来源。')
                    concise = [{**history[0], 'content': history[0]['content'] + notice}, history[1]]
                    if history_size(concise) <= char_limit and request_tokens(concise) <= session_limit('MODEL_INPUT_TOKENS'):
                        history = concise
                    if not self.reserve_round():
                        self.blocked = '独立复核尚无有效判定且模型交互轮次已用完，草稿尚未批准。'
                        event['status'] = retry_status
                        return self.blocked
                    run['recovery_attempts'] = run.get('recovery_attempts', 0) + 1
                attempts.append('pending')
                try:
                    answer = await self.manager._call_model(history, disable_tools=True,
                                                            session_id=self.session.id, emit=emit, **review_options)
                except TimeoutError:
                    retry_status = attempts[-1] = 'TimeoutError'
                    can_retry = (not attempt and not (recovery and recovery[0])
                                 and session_limit('MODEL_RECOVERY_LIMIT') > 0
                                 and (dispatch_allowed is None or dispatch_allowed()))
                    if not can_retry:
                        raise
                    if recovery is not None:
                        recovery[0] = True
                    continue
                if self.session.cancelled.is_set():
                    raise asyncio.CancelledError
                verdict = parse_review_reply(answer)
                if targets:
                    citations = {} if state_key is not None else None
                    feedback, invalid = claim_assessment(verdict, sources, targets, passages, citations)
                    verified_verdict = invalid is None
                else:
                    feedback, verified_verdict = assess_review(verdict, sources)
                    invalid = None
                if invalid:
                    event.setdefault('invalid_reasons', []).append(invalid)
                    retry_status = attempts[-1] = 'invalid_verdict'
                    can_retry = (not attempt and not (recovery and recovery[0])
                                 and session_limit('MODEL_RECOVERY_LIMIT') > 0
                                 and (dispatch_allowed is None or dispatch_allowed()))
                    if can_retry:
                        if recovery is not None:
                            recovery[0] = True
                        continue
                    event['status'] = retry_status
                    self.blocked = INVALID_REVIEW
                    return self.blocked
                break
            if verified_verdict and feedback and verdict.get('status') == 'need_source':
                self.needs_sources = True
            if verified_verdict and feedback and verdict.get('status') in {'revise', 'need_source'}:
                event['verdict_status'] = verdict['status']
            if (feedback and verdict and verdict.get('status') == 'revise' and targets
                    and state_key is not None and len(draft) <= 32_000):
                selected_ids = {issue.get('target_id') for issue in verdict['issues']}
                selected = [{**target, '_cited_spans': (citations or {}).get(target['id'], [])}
                            for target in targets if target['id'] in selected_ids]
                if selected and len(self._revisions) < 16:
                    self._revisions[state_key] = (fingerprint, draft, copy.deepcopy(selected))
            if feedback and self.needs_sources and pending_paths:
                feedback += ' 可用已有只读工具核对本轮资料工具传入的路径：' + json.dumps(pending_paths[:3], ensure_ascii=False)
            attempts[-1] = event['status'] = 'revise' if feedback else 'passed'
            if stage == 'handoff':
                self.handoff_feedback_valid = 'verdict_status' in event
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
            if unit_kind in {'item', 'shared_claim'} and isinstance(exc, ModelDispatchStopped):
                return '交接复核已停止，草稿尚未批准。'
            self.blocked = '来源复核未能完成，草稿尚未提交。'
            return self.blocked
        finally:
            if phase_session is None:
                self.session.phase = previous_phase
                self.manager.notify()
