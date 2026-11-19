"""Flow 出站草稿的独立模型复核，原文与推理均不写入诊断记录。"""
import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Callable

from agent_core.core.budgets import BudgetExceeded
from agent_core.core.context import history_size, request_tokens
from agent_core.core.flow_draft_review import (
    AmbiguousDraft, needs_review, normalised_draft, parse_review_reply, review_feedback, review_messages,
)
from agent_core.core.flow_review_claims import (
    INVALID_REVIEW, action_review_context, claim_assessment, claim_targets, focal_targets,
    prepare_claim_request, shared_targets,
)
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
        self.pending_items: set[str] = set()
        self.passed_items: set[str] = set()
        # 仅本 Gate 实例保存有限的上次争议，不写入会话或审计记录。
        self._revisions: dict[object, tuple[str, str, list[dict]]] = {}

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
        if evidence['source'] == 'executed' and tool_outcome(result) == 'completed':
            self.pending_tools.discard(key)
        return result

    async def check(self, draft: str, *, stage: str, force: bool = False, call_id: str | None = None,
                    action_context: dict | None = None, revision_key: object = None,
                    revision_definition: dict | None = None) -> str | None:
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
        if not force and not self.pending_items and not needs_review(draft):
            return None
        from agent_core.core.flow_review_items import review_units
        char_limit = session_limit('MODEL_INPUT_CHARS')
        sources = review_sources(self.session.record, char_limit=min(24000, char_limit // 2))
        units = review_units(draft, sources, required_ids=frozenset(self.pending_items)) if sources else None
        # 无法把先前整稿拒绝关联到事项时，保留整稿复审。
        if units is None or (force and not self.pending_items):
            return await self._check_one(draft, stage=stage, force=force or bool(self.pending_items), call_id=call_id,
                                         recheck=force or bool(self.pending_items))
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
            return await self._check_one(draft, stage=stage, force=True, call_id=call_id, recheck=True)
        if not units:
            return None
        events = self.session.record['last_run'].setdefault('draft_reviews', [])
        start = len(events)
        recovery = [False]
        summary = []
        feedback = None
        cancelled = False
        for unit in units:
            item_id = unit['item_id']
            kind = unit.get('kind', 'item')
            key = hashlib.sha256(json.dumps([kind, item_id, unit['draft'], unit['sources'], self.session.flow_review_task],
                ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            item_digest = hashlib.sha256(item_id.encode('utf-8', 'surrogatepass')).hexdigest()
            if key in self.passed_items and item_id not in self.pending_items:
                summary.append({'item_sha256': item_digest, 'status': 'reused', 'draft_sha256': hashlib.sha256(
                    unit['draft'].encode('utf-8', 'surrogatepass')).hexdigest()})
                continue
            try:
                feedback = await self._check_one(unit['draft'], stage=stage, force=True,
                    sources_override=unit['sources'], unit_kind=kind, recovery=recovery,
                    recheck=item_id in self.pending_items, revision_key=(kind, item_id))
            except asyncio.CancelledError:
                cancelled = True
            child = events.pop()
            summary.append({'item_sha256': item_digest, 'kind': kind, 'status': child['status'],
                            'target_count': child.get('target_count', 0),
                            'invalid_reasons': child.get('invalid_reasons', []),
                            'draft_sha256': child['draft_sha256'], 'attempts': child.get('attempts', [])})
            if cancelled:
                break
            if feedback:
                self.pending_items.add(item_id)
                break
            self.pending_items.discard(item_id)
            self.passed_items.add(key)
        event = {'stage': stage, 'status': summary[-1]['status'] if feedback or cancelled else 'passed',
                 'draft_sha256': hashlib.sha256(draft.encode('utf-8', 'surrogatepass')).hexdigest(),
                 'unit_count': len(units), 'units': summary,
                 'attempts': [attempt for row in summary for attempt in row.get('attempts', [])]}
        events.insert(start, event)
        if cancelled:
            raise asyncio.CancelledError
        return feedback

    async def _check_one(self, draft: str, *, stage: str, force: bool = False, call_id: str | None = None,
                         sources_override: list[dict] | None = None, unit_kind: str | None = None,
                         recovery: list[bool] | None = None, recheck: bool = False,
                         action_context: dict | None = None, revision_key: object = None,
                         revision_definition: dict | None = None) -> str | None:
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
                   review_sources(self.session.record, char_limit=min(24000, char_limit // 2)))
        if stage in {'tool', 'preview'}:
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
        fingerprint = hashlib.sha256(json.dumps([sources, task, action_context, unit_kind, revision_definition],
            ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        shared = unit_kind == 'shared_claim'
        targets = shared_targets(draft) if shared else claim_targets(draft)
        focused = None
        if recheck and previous is not None and previous[0] == fingerprint:
            from agent_core.core.flow_review_revision import revision_targets
            focused = revision_targets(previous[1], previous[2], draft)
        if focused is not None:
            targets = focused
        elif not shared and not recheck:
            candidates = targets
            targets = focal_targets(candidates)
            event['background_count'] = len(candidates) - len(targets)
        event['target_count'] = len(targets)
        passages = []
        if targets:
            history, passages = prepare_claim_request(
                draft, sources, targets=targets, node_task=task, per_item=unit_kind == 'item', shared=shared,
                action_context=action_context, char_limit=char_limit, token_limit=session_limit('MODEL_INPUT_TOKENS'))
        else:
            history = review_messages(draft, sources, node_task=task, per_item=unit_kind == 'item')
        event['passage_count'] = len(passages)
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
            retry_status = None
            for attempt in range(2):
                if self.session.cancelled.is_set():
                    raise asyncio.CancelledError
                if attempt:
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
                    if recovery is not None:
                        recovery[0] = True
                    run['recovery_attempts'] = run.get('recovery_attempts', 0) + 1
                can_retry = not attempt and not (recovery and recovery[0]) and session_limit('MODEL_RECOVERY_LIMIT') > 0
                attempts.append('pending')
                try:
                    answer = await self.manager._call_model(history, disable_tools=True,
                                                            session_id=self.session.id, emit=emit)
                except TimeoutError:
                    retry_status = attempts[-1] = 'TimeoutError'
                    if not can_retry:
                        raise
                    continue
                if self.session.cancelled.is_set():
                    raise asyncio.CancelledError
                verdict = parse_review_reply(answer)
                if targets:
                    feedback, invalid = claim_assessment(verdict, sources, targets, passages)
                else:
                    feedback, invalid = review_feedback(verdict, sources), None
                if invalid:
                    event.setdefault('invalid_reasons', []).append(invalid)
                    retry_status = attempts[-1] = 'invalid_verdict'
                    if can_retry:
                        continue
                    event['status'] = retry_status
                    self.blocked = INVALID_REVIEW
                    return self.blocked
                break
            if feedback and verdict and verdict.get('status') == 'need_source':
                self.needs_sources = True
            if (feedback and verdict and verdict.get('status') == 'revise' and targets
                    and state_key is not None and len(draft) <= 32_000):
                selected_ids = {issue.get('target_id') for issue in verdict['issues']}
                selected = [target for target in targets if target['id'] in selected_ids]
                if selected and len(self._revisions) < 16:
                    self._revisions[state_key] = (fingerprint, draft, copy.deepcopy(selected))
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
