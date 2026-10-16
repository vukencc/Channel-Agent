"""有界、只读运行追踪；不收集模型内容、工具参数和异常正文。"""
import asyncio
import inspect
import math
import re
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps


_binding = ContextVar('debug_binding', default=None)
_parent = ContextVar('debug_parent', default=None)
_causal = ContextVar('debug_causal', default=None)

# This is a metadata schema, never a general logging or string redaction API.
_EVENT_KINDS = frozenset({
    'turn.started', 'turn.stopped', 'model.completed', 'model.final', 'tools.batch',
    'tool.started', 'tool.completed', 'phase.changed', 'agent.created', 'agent.started',
    'agent.completed', 'message.sent', 'message.read', 'context.sample',
    'context.compacted', 'plan.updated', 'span.started', 'span.completed',
})
_ID_FIELDS = frozenset({
    'turn_id', 'batch_id', 'tool_call_id', 'task_id', 'parent_session_id',
    'related_session_id', 'span_id', 'parent_span_id',
})
_INTEGER_FIELDS = frozenset({'message_seq', 'message_index', 'stage_index'})
_STATES = frozenset({
    'planned', 'pending', 'queued', 'running', 'completed', 'cancelled', 'error',
    'rejected', 'timeout', 'checkpoint', 'stopped', 'created', 'idle',
    'compacting', 'compacted',
})
_REASONS = frozenset({
    'completed', 'cancelled', 'user_cancelled', 'user_stop', 'stopped', 'error',
    'timeout', 'rejected', 'checkpoint', 'max_steps', 'budget_exceeded',
    'token_limit', 'context_limit', 'compaction', 'task_switch', 'phase_change',
    'unknown', 'prepared', 'before_compact', 'after_compact', 'context_budget',
    'tool_budget', 'task_continuation_limit', 'plan_waiting', 'no_execution_progress',
    'plan_evidence_capacity', 'model_connection', 'cost_or_rate_budget', 'finished',
})
_PHASES = frozenset({
    'model_wait', 'context_prepare', 'compaction', 'tools', 'continuation',
    'recovery', 'confirmation', 'stopping', 'planning', 'complete',
})
_METRIC_FIELDS = frozenset({
    'input_tokens', 'output_tokens', 'context_utilization', 'context_tokens',
    'context_limit_tokens', 'context_chars', 'context_limit_chars', 'input_cost',
    'output_cost', 'duration_ms', 'tool_count', 'error_count', 'total_tokens',
})
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z')


def _identifier(value):
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
        return value
    if type(value) is int and 0 <= value <= 2 ** 63 - 1:
        return value
    return None


def _number(value):
    if type(value) is int:
        return 0 <= value <= 2 ** 63 - 1
    return type(value) is float and math.isfinite(value) and value >= 0


def _label(value):
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else ''


def _metadata(fields, *, causal_only=False):
    """Copy only fixed metadata fields; never stringify caller supplied values."""
    result = {}
    for key in _ID_FIELDS:
        value = _identifier(fields.get(key))
        if value is not None:
            result[key] = value
    for key in _INTEGER_FIELDS:
        value = fields.get(key)
        if type(value) is int and 0 <= value <= 2 ** 63 - 1:
            result[key] = value
    if causal_only:
        return result
    for key, choices in (('state', _STATES), ('reason', _REASONS), ('phase', _PHASES)):
        value = fields.get(key)
        if isinstance(value, str) and value in choices:
            result[key] = value
    tool_name = fields.get('tool_name')
    if _label(tool_name):
        result['tool_name'] = tool_name
    duration = fields.get('duration_ms')
    if _number(duration):
        result['duration_ms'] = duration
    metrics = fields.get('metrics')
    if isinstance(metrics, dict):
        safe = {}
        for key in _METRIC_FIELDS:
            value = metrics.get(key)
            if _number(value):
                safe[key] = value
        source = metrics.get('tokens_source')
        if isinstance(source, str) and source in {'provider', 'estimated', 'estimate', 'unknown'}:
            safe['tokens_source'] = 'estimated' if source == 'estimate' else source
        if safe:
            result['metrics'] = safe
    return result


class Span:
    def __init__(self, identifier=None):
        self.id = identifier
        self.outcome = 'completed'


def tool_outcome(result: str) -> str:
    """只判断现有工具协议的状态前缀，不保存返回正文。"""
    if result.startswith(('[超时]', '[已取消]', '[取消]')):
        return 'timeout' if result.startswith('[超时]') else 'cancelled'
    if result.startswith(('[拒绝]', '[已拒绝]', '[已拦截]', '未知工具:')):
        return 'rejected'
    if result.startswith(('[失败]', '[错误]', '工具执行失败:')):
        return 'error'
    if result.startswith('[退出码]'):
        first_line = result.partition('\n')[0].rstrip('\r')
        exit_status = re.fullmatch(r'\[退出码\] (-?[0-9]+)', first_line)
        if exit_status is not None:
            # Avoid parsing an unbounded integer from an arbitrary tool result.
            return 'completed' if exit_status[1].lstrip('-0') == '' else 'error'
    return 'completed'


class DebugRecorder:
    def __init__(self, enabled: bool, capacity: int = 512):
        self.enabled = enabled
        self.capacity = max(1, min(int(capacity), 10000))
        self._events = deque(maxlen=self.capacity)
        self._timeline = deque(maxlen=self.capacity)
        self._active = {}
        self._lock = threading.Lock()
        self._revision = 0
        self._next_id = 0
        self._dropped = 0
        self._seq = 0
        self.generation = uuid.uuid4().hex
        self._started = time.monotonic()

    @contextmanager
    def scope(self, session_id, **ids):
        if not self.enabled:
            yield
            return
        previous = _binding.get()
        token = _binding.set((self, session_id))
        parent_token = _parent.set(None) if previous != (self, session_id) else None
        causal = dict(_causal.get() or {}) if previous == (self, session_id) else {}
        causal.update(_metadata(ids, causal_only=True))
        causal_token = _causal.set(causal)
        try:
            yield
        finally:
            if parent_token is not None:
                _parent.reset(parent_token)
            _causal.reset(causal_token)
            _binding.reset(token)

    def _publish_locked(self, kind, session_id, fields):
        """Assign sequence and both clocks under the same lock as publication."""
        self._seq += 1
        row = {'seq': self._seq, 'kind': kind, 'ts': time.time(),
               'elapsed_ms': round((time.monotonic() - self._started) * 1000, 3)}
        safe_session = _identifier(session_id)
        if safe_session is not None:
            row['session_id'] = safe_session
        row.update(fields)
        if len(self._timeline) == self.capacity:
            self._dropped += 1
        self._timeline.append(row)
        self._revision += 1
        return self._seq

    def event(self, kind: str, *, session_id=None, **fields) -> int | None:
        """Publish allowed metadata only; a disabled recorder does no event work."""
        if not self.enabled or not isinstance(kind, str) or kind not in _EVENT_KINDS:
            return None
        binding = _binding.get()
        if session_id is None and binding is not None and binding[0] is self:
            session_id = binding[1]
        causal = dict(_causal.get() or {}) if binding == (self, session_id) else {}
        causal.update(_metadata(fields))
        if binding == (self, session_id) and _parent.get() is not None:
            causal.setdefault('span_id', _parent.get())
        with self._lock:
            return self._publish_locked(kind, session_id, causal)

    def _span_metadata(self, row):
        result = _metadata(row)
        result.update(module=row['module'], name=row['name'],
                      started_at=row['started_at'], duration_ms=row['duration_ms'])
        if row['error_type']:
            result['error_type'] = row['error_type']
        return result

    @contextmanager
    def span(self, module: str, name: str = ''):
        binding = _binding.get()
        if not self.enabled or binding is None or binding[0] is not self:
            yield Span()
            return
        started = time.monotonic()
        with self._lock:
            if len(self._active) >= self.capacity:
                self._dropped += 1
                self._revision += 1
                identifier = None
            else:
                self._next_id += 1
                identifier = self._next_id
                row = {'id': identifier, 'parent_id': _parent.get(), 'session_id': _identifier(binding[1]),
                       'module': _label(module), 'name': _label(name), 'state': 'running',
                       'started_at': time.time(), 'duration_ms': 0, 'error_type': ''}
                row.update(_causal.get() or {})
                row.update(span_id=identifier, parent_span_id=_parent.get())
                self._active[identifier] = (row, started)
                self._revision += 1
                self._publish_locked('span.started', binding[1], self._span_metadata(row))
        handle = Span(identifier)
        token = _parent.set(identifier) if identifier is not None else None
        error_type = ''
        try:
            yield handle
        except BaseException as exc:
            handle.outcome = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error'
            error_type = type(exc).__name__[:80]
            raise
        finally:
            if token is not None:
                _parent.reset(token)
            if identifier is not None:
                with self._lock:
                    row, _ = self._active.pop(identifier)
                    outcome = handle.outcome if isinstance(handle.outcome, str) and handle.outcome in _STATES else 'error'
                    row.update(state=outcome, duration_ms=round((time.monotonic() - started) * 1000, 2),
                               error_type=error_type)
                    self._events.append(row)
                    self._revision += 1
                    self._publish_locked('span.completed', binding[1], self._span_metadata(row))

    def snapshot(self, *, cursor: int | None = None, generation: str | None = None):
        """Read a consistent window; active spans are always a complete snapshot."""
        with self._lock:
            now = time.monotonic()
            oldest = self._timeline[0]['seq'] if self._timeline else self._seq + 1
            requested = cursor is not None
            valid_cursor = type(cursor) is int and 0 <= cursor <= self._seq
            same_generation = generation is None or generation == self.generation
            gap = requested and same_generation and valid_cursor and cursor < oldest - 1
            reset = (generation is not None and not same_generation) or (requested and (not valid_cursor or gap))
            incremental = requested and same_generation and valid_cursor and not reset
            timeline = [row for row in self._timeline if not incremental or row['seq'] > cursor]
            return {'enabled': self.enabled, 'revision': self._revision, 'dropped': self._dropped,
                    'capacity': self.capacity, 'events': deepcopy(list(self._events)),
                    'active': deepcopy([{**row, 'duration_ms': round((now - started) * 1000, 2)}
                                        for row, started in self._active.values()]),
                    'generation': self.generation, 'cursor': self._seq, 'oldest_seq': oldest,
                    'now_elapsed_ms': round((now - self._started) * 1000, 3),
                    'gap': bool(gap), 'reset': bool(reset), 'incremental': bool(incremental),
                    'timeline': deepcopy(timeline)}

    def activity(self, session_id: str, kind: str):
        """流事件只更新固定状态码，不为每个 token 创建事件。"""
        if not self.enabled or kind not in {'reasoning', 'content', 'tool_arguments', 'status'}:
            return
        with self._lock:
            for row, _ in self._active.values():
                if row['session_id'] == session_id and row['module'] == 'model.request':
                    row.update(activity=kind, last_activity_at=time.time())
                    self._revision += 1

    def track_future(self, future, session_id: str, module: str):
        """跨线程 Future 只登记完成回调，计时含 writer 队列等待。"""
        if not self.enabled:
            return
        started = time.monotonic()
        binding = _binding.get()
        with self._lock:
            if len(self._active) >= self.capacity:
                self._dropped += 1
                self._revision += 1
                return
            self._next_id += 1
            identifier = self._next_id
            row = {'id': identifier, 'parent_id': _parent.get() if binding == (self, session_id) else None,
                   'session_id': _identifier(session_id), 'module': _label(module), 'name': '', 'state': 'running',
                   'started_at': time.time(), 'duration_ms': 0, 'error_type': ''}
            if binding == (self, session_id):
                row.update(_causal.get() or {})
            row.update(span_id=identifier, parent_span_id=row['parent_id'])
            self._active[identifier] = (row, started)
            self._revision += 1
            self._publish_locked('span.started', session_id, self._span_metadata(row))
        def finished(done):
            error = None if done.cancelled() else done.exception()
            with self._lock:
                row, _ = self._active.pop(identifier)
                row.update(state='cancelled' if done.cancelled() else 'error' if error else 'completed',
                           error_type=type(error).__name__[:80] if error else '',
                           duration_ms=round((time.monotonic() - started) * 1000, 2))
                self._events.append(row)
                self._revision += 1
                self._publish_locked('span.completed', session_id, self._span_metadata(row))
        future.add_done_callback(finished)


@contextmanager
def debug_context(**ids):
    """Temporarily attach validated causal IDs to the current recorder scope."""
    binding = _binding.get()
    if binding is None or not binding[0].enabled:
        yield
        return
    causal = dict(_causal.get() or {})
    causal.update(_metadata(ids, causal_only=True))
    token = _causal.set(causal)
    try:
        yield
    finally:
        _causal.reset(token)


def debug_event(kind: str, **fields) -> int | None:
    """Publish metadata in the current Agent scope without changing its work."""
    binding = _binding.get()
    if binding is None or not binding[0].enabled:
        return None
    return binding[0].event(kind, **fields)


@contextmanager
def debug_span(module: str, name: str = ''):
    """在所属 Agent 的调用域内记录；线程继承 ContextVar，不改工具语义。"""
    binding = _binding.get()
    if binding is None:
        yield Span()
    else:
        with binding[0].span(module, name) as handle:
            yield handle


def traced(module: str):
    """装饰 manager 或具有 manager 属性的异步模块边界。"""
    def decorate(function):
        @wraps(function)
        async def wrapped(owner, *args, **kwargs):
            manager = getattr(owner, 'manager', owner)
            recorder = getattr(manager, 'debug', None)
            if recorder is None or not recorder.enabled:
                return await function(owner, *args, **kwargs)
            session = args[0] if args and hasattr(args[0], 'record') else None
            session_id = session.id if session is not None else kwargs.get('session_id', '')
            turn_id = session.record.get('last_run', {}).get('turn_id') if session is not None else None
            with recorder.scope(session_id, turn_id=turn_id), recorder.span(module) as span:
                result = await function(owner, *args, **kwargs)
                if module == 'tool.total' and isinstance(result, str):
                    span.outcome = tool_outcome(result)
                if module == 'agent.run' and session is not None:
                    status = session.record.get('status')
                    span.outcome = status if status in {'error', 'cancelled', 'checkpoint'} else 'completed'
                return result
        return wrapped
    return decorate


def trace_function(module: str):
    """普通函数继承当前 Agent 域；保持原返回值和异常。"""
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with debug_span(module):
                    return await function(*args, **kwargs)
            return asynchronous
        @wraps(function)
        def synchronous(*args, **kwargs):
            with debug_span(module) as span:
                result = function(*args, **kwargs)
                if module == 'permission.confirm' and result is False:
                    span.outcome = 'rejected'
                return result
        return synchronous
    return decorate
