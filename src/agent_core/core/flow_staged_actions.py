"""将当前 Flow 轮次的动作草稿、来源复核和原工具正式调用关联起来。"""
import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone

from agent_core import config
from agent_core.core.audit_writer import append_audit
from agent_core.core.flow_action_preflight import FlowActionPreflight
from agent_core.core.flow_review_claims import action_review_context
from agent_core.core.flow_review_sources import review_sources
from agent_core.core.instrumentation import tool_outcome
from agent_core.core.session_limits import limit as session_limit
from agent_core.core.storage import wait_for_io_completion
from agent_core.core.tool_settings import bounded_schemas
from agent_core.tools import TOOL_REGISTRY


class FlowStagedActions:
    def __init__(self, gate):
        self.gate = gate
        self.manager = gate.manager
        self.session = gate.session
        # 分支和委派继承 Flow 工具边界，不需要获得父节点的项目归属或预约权。
        record = self.session.record
        self.requires_preview = bool(record.get('flow_project_id') or record.get('flow_workspace_scope')
                                     or record.get('delegated_from') or record.get('branch'))
        self.definitions = {}
        self.preflight = FlowActionPreflight([])
        self.issued = frozenset()
        self.previewed = set()
        self.receipts = {}
        self.unresolved = set()
        self.receipt_binding = None
        self.audit_failed = False
        self._audit_queue = []
        self._audit_lock = asyncio.Lock()
        self._receipt_events = {}

    def _queue_audit(self, event: dict, stage: str, status: str) -> None:
        self._audit_queue.append({'time': datetime.now(timezone.utc).isoformat(), 'event': 'flow_action',
                                  'name': event['tool'], 'id': event['tool_call_id'], 'stage': stage,
                                  'session_id': self.session.id, 'turn_id': event.get('turn_id'),
                                  'tool_name': event['tool'], 'tool_call_id': event['tool_call_id'],
                                  'status': status, 'hash': event['arguments_sha256'],
                                  'draft_id': event.get('draft_id')})

    async def flush_audit(self) -> bool:
        """持久化已排队事件；失败后阻断本轮，不自动重试。"""
        async with self._audit_lock:
            return await self._flush_audit_locked()

    async def _flush_audit_locked(self) -> bool:
        if self.audit_failed:
            self._audit_queue.clear()
            return False
        while self._audit_queue:
            row = self._audit_queue.pop(0)
            try:
                path = self.manager.store.directory(self.session.id) / 'audit.jsonl'
                task = asyncio.create_task(asyncio.to_thread(append_audit, path,
                    json.dumps(row, ensure_ascii=True, separators=(',', ':')), sync=config.AUDIT_SYNC))
                await wait_for_io_completion(task)
            except Exception:
                self.audit_failed = True
                self._audit_queue.clear()
                self.gate.blocked = ('动作审计写入失败，业务状态待核对，请检查原工具结果，勿自动重发。'
                                     if row['stage'] == 'commit_result' and row.get('dispatched')
                                     else '动作审计写入失败，尚未执行的业务已阻断，请核对审计和状态。')
                return False
        return True

    @property
    def pending(self) -> bool:
        return bool(self.unresolved or self.receipts)

    def _authorized(self):
        allowed = self.manager._effective_session_tools(self.session)
        return {name: tool.definition for name, tool in self.manager._session_custom_registry(self.session).items()
                if allowed is None or name in allowed}

    def model_schemas(self, original: list[dict]) -> list[dict]:
        """预算和实际请求使用相同的授权目录。变更权限或定义将撤销已有批准。"""
        authorized = self._authorized()
        if self.definitions != authorized:
            for identifier in self.receipts:
                self._queue_audit(self._receipt_events.pop(identifier), 'preview', 'authorization_changed')
            self.preflight = FlowActionPreflight(list(authorized.values()),
                occupied_names=set(TOOL_REGISTRY) | set(self.manager._session_custom_registry(self.session)),
                stage_selectors=self.requires_preview)
            self.definitions = authorized
            self.unresolved.update(self.receipts.values())
            self.receipts.clear()
            self.issued = frozenset()
        if self.receipts:
            self._expire_changed(self._binding())
        if not authorized:
            return original
        additions = self.preflight.schemas()
        names = [row['function']['name'] for row in additions]
        return [row for row in original if row['function']['name'] not in authorized] + bounded_schemas(additions, names)

    def begin_request(self) -> None:
        self.issued = self.preflight.approved_ids()

    def is_preview(self, name: str) -> bool:
        return self.preflight.original_for_preview(name) is not None

    def _expire_changed(self, binding: dict) -> None:
        if self.receipts and binding != self.receipt_binding:
            self.unresolved.update(self.receipts.values())
            for identifier in self.receipts:
                self._queue_audit(self._receipt_events.pop(identifier), 'preview', 'context_changed')
                self.preflight.discard(identifier)
            self.receipts.clear()
        self.receipt_binding = binding

    def _binding(self) -> dict:
        session = self.session
        record = session.record
        sources = review_sources(record, char_limit=min(24000, session_limit('MODEL_INPUT_CHARS') // 2))
        encoded = json.dumps(sources, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        digest = hashlib.sha256(encoded.encode('ascii')).hexdigest()
        return {'session': session.id, 'turn': record['last_run']['turn_id'], 'node': record.get('flow_node_id'),
                'task': session.flow_review_task, 'source_digest': digest, 'model': record.get('model_profile'),
                'policy': self.manager.permission_override or record.get('permission_policy'),
                'environment': self.manager.execution_environment(session), 'allowed': self.manager._effective_session_tools(session)}

    def _event(self, call: dict, name: str, stage: str) -> dict:
        event = {'stage': stage, 'tool': name, 'tool_call_id': call['id'], 'status': 'pending',
                 'turn_id': self.session.record['last_run'].get('turn_id'),
                 'arguments_sha256': hashlib.sha256(call['function']['arguments'].encode('utf-8', 'surrogatepass')).hexdigest()}
        self.session.record['last_run'].setdefault('action_preflights', []).append(event)
        return event

    async def _preview(self, call: dict, name: str, evidence: dict) -> str:
        fn = call['function']
        event = self._event(call, name, 'preview')
        evidence['action_stage'] = 'preview'
        self.previewed.add(name)
        key = self.gate.tool_key(name, fn['arguments'])
        self.unresolved.add(key)
        definition = self._authorized().get(name)
        if definition is None or definition != self.definitions.get(name):
            event['status'] = 'authorization_changed'
            return '[已拒绝] 工具选择或定义已变化，请重新查看可用工具。'
        binding = self._binding()
        self._expire_changed(binding)
        try:
            identifier = self.preflight.draft(fn['name'], fn['arguments'], binding)
        except ValueError as exc:
            event['status'] = 'invalid_draft'
            return f'[已拒绝] 草稿未获受理：{exc}。'
        event['draft_id'] = identifier
        # 新草稿校验通过后才撤销同事项的旧批准。
        if key != self.gate.tool_key(name, '{}'):
            for prior, prior_key in list(self.receipts.items()):
                if prior_key == key:
                    self._queue_audit(self._receipt_events.pop(prior), 'preview', 'superseded')
                    self.preflight.discard(prior)
                    del self.receipts[prior]
        try:
            feedback = await self.gate.check(fn['arguments'], stage='preview', force=key in self.gate.pending_tools,
                call_id=call['id'], revision_key=key, revision_definition=definition,
                action_context=action_review_context(definition))
            if self.session.cancelled.is_set():
                raise asyncio.CancelledError
            if feedback:
                self.gate.pending_tools.add(key)
                event['status'] = 'review_blocked' if self.gate.blocked else 'needs_revision'
                return '[已拒绝] 草稿待修订，业务操作尚未执行。' + feedback
            if (self._authorized().get(name) != definition
                    or not self.preflight.approve(identifier, self._binding())):
                event['status'] = 'context_changed'
                return '[已拒绝] 草稿的来源或权限已变化，请重新预审。'
            self.receipts[identifier] = key
            self._receipt_events[identifier] = dict(event)
            self.unresolved.discard(key)
            evidence['approval_progress'] = True
            event['status'] = 'approved'
            return (f'草稿已批准，业务操作尚未执行。下一轮调用原工具 {name}，原样复制本次草稿参数；'
                    '收到正式工具的执行结果后再写交接。')
        except asyncio.CancelledError:
            event['status'] = 'cancelled'
            raise
        finally:
            if event['status'] != 'approved':
                self.preflight.discard(identifier)

    async def execute(self, call: dict, evidence: dict) -> str:
        if not await self.flush_audit():
            return '[已拒绝] ' + self.gate.blocked
        before = len(self.session.record['last_run'].get('action_preflights', []))
        try:
            result = await self._execute(call, evidence)
        except BaseException:
            events = self.session.record['last_run'].get('action_preflights', [])
            event = next((row for row in events[before:] if row['tool_call_id'] == call['id']), None)
            if event is not None:
                self._queue_audit(event, 'preview' if event['stage'] == 'preview' else 'commit_result', event['status'])
                self._audit_queue[-1]['dispatched'] = event.get('_dispatched', False)
                await self.flush_audit()
            raise
        events = self.session.record['last_run'].get('action_preflights', [])
        event = next((row for row in events[before:] if row['tool_call_id'] == call['id']), None)
        if event is not None:
            self._queue_audit(event, 'preview' if event['stage'] == 'preview' else 'commit_result', event['status'])
            self._audit_queue[-1]['dispatched'] = event.get('_dispatched', False)
            if not await self.flush_audit():
                identifier = event.get('draft_id')
                if event['stage'] == 'preview' and identifier:
                    self.preflight.discard(identifier)
                    self.receipts.pop(identifier, None)
                    self._receipt_events.pop(identifier, None)
                return '[失败] 审计失败：' + self.gate.blocked
        return result

    async def _execute(self, call: dict, evidence: dict) -> str:
        fn = call['function']
        name = fn['name']
        original = self.preflight.original_for_preview(name)
        if original is not None:
            return await self._preview(call, original, evidence)
        preview = self.preflight.preview_for_original(name)
        if not preview:
            if self.requires_preview and name not in TOOL_REGISTRY and name in self._authorized():
                event = self._event(call, name, 'commit')
                event['status'] = 'authorization_changed'
                evidence['action_stage'] = 'commit'
                self.unresolved.add(self.gate.tool_key(name, fn['arguments']))
                return '[已拒绝] 本轮模型请求没有该动作的预审入口，请刷新工具列表后先提交草稿。'
            return await self.gate.execute_tool(call, evidence)
        # 旧版无项目绑定的直接调用仍经过原独立核验。使用预审协议后不得切回直接调用。
        if not self.requires_preview and name not in self.previewed:
            return await self.gate.execute_tool(call, evidence)
        evidence['action_stage'] = 'commit'
        event = self._event(call, name, 'commit')
        key = self.gate.tool_key(name, fn['arguments'])
        self.unresolved.add(key)
        definition = self._authorized().get(name)
        policy = self.manager.permission_override or self.session.record.get('permission_policy')
        # 动态工具最终通过 run_command；readonly 策略在沙箱中一律拒绝该命令。
        if self.gate.blocked or self.session.cancelled.is_set() or not definition or policy == 'readonly':
            event['status'] = 'blocked'
            return '[已拒绝] 当前节点、复核或工具权限不允许正式提交。'
        binding = copy.deepcopy(self._binding())
        definition = copy.deepcopy(definition)
        identifier = self.preflight.reserve_approval(name, fn['arguments'], binding, definition, issued=self.issued)
        if not identifier:
            event['status'] = 'not_approved'
            return f'[已拒绝] 本次提案没有上一轮对相同工具与参数的有效批准。请先调用 {preview}。'
        self.receipts.pop(identifier, None)
        self._receipt_events.pop(identifier, None)
        event['draft_id'] = identifier
        event['status'] = 'dispatching'
        try:
            self._queue_audit(event, 'commit_intent', 'dispatching')
            if not await self.flush_audit():
                event['status'] = 'audit_failed'
                return '[已拒绝] ' + self.gate.blocked
            if self.session.cancelled.is_set():
                event['status'] = 'cancelled'
                raise asyncio.CancelledError
            if self._authorized().get(name) != definition:
                event['status'] = 'authorization_changed'
                return '[已拒绝] 工具选择或定义已变化，业务操作尚未执行。'
            if self._binding() != binding:
                event['status'] = 'context_changed'
                return '[已拒绝] 来源或权限已变化，业务操作尚未执行。'
            if self.gate.blocked:
                event['status'] = 'blocked'
                return '[已拒绝] 当前复核状态不允许正式提交。'
            if not self.preflight.consume_reservation(identifier):
                event['status'] = 'not_approved'
                return '[已拒绝] 批准已失效，业务操作尚未执行。'
        finally:
            self.preflight.discard(identifier)
        event['_dispatched'] = True
        try:
            result = await self.manager._tool(self.session, call, evidence=evidence)
        except asyncio.CancelledError:
            event['status'] = 'cancelled'
            raise
        except Exception:
            event['status'] = 'execution_error'
            raise
        event['status'] = tool_outcome(result)
        if evidence.get('source') == 'executed' and event['status'] == 'completed':
            self.unresolved.discard(key)
            self.gate.pending_tools.discard(key)
        return result
