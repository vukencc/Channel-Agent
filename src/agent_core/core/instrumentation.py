"""中立运行观测接口；Core 默认不记录，采集器由宿主显式注入。"""
import inspect
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, ContextManager, Iterator, Protocol, runtime_checkable


class Span:
    """可由调用方标注结果的中立操作句柄。"""

    def __init__(self, identifier: int | None = None):
        self.id = identifier
        self.outcome = 'completed'


@runtime_checkable
class RuntimeObserver(Protocol):
    """运行时仅发送元数据，不依赖采集器的存储或展示实现。"""

    enabled: bool

    def scope(self, session_id: str, **ids: Any) -> ContextManager[None]: ...

    def span(self, module: str, name: str = '') -> ContextManager[Span]: ...

    def event(self, kind: str, *, session_id: str | None = None, **fields: Any) -> int | None: ...

    def activity(self, session_id: str, kind: str) -> None: ...

    def track_future(self, future: Any, session_id: str, module: str) -> None: ...


class NullObserver:
    """默认空实现：不读取 DEBUG、不分配事件存储、不注册回调。"""

    enabled = False

    @contextmanager
    def scope(self, session_id: str, **ids: Any) -> Iterator[None]:
        yield

    @contextmanager
    def span(self, module: str, name: str = '') -> Iterator[Span]:
        yield Span()

    def event(self, kind: str, *, session_id: str | None = None, **fields: Any) -> None:
        return None

    def activity(self, session_id: str, kind: str) -> None:
        return None

    def track_future(self, future: Any, session_id: str, module: str) -> None:
        return None


# 观测适配器与 Core 装饰器共享同一组调用域；线程通过 copy_context 继承。
_binding: ContextVar[tuple[RuntimeObserver, str] | None] = ContextVar('runtime_binding', default=None)
_parent: ContextVar[int | None] = ContextVar('runtime_parent', default=None)
_causal: ContextVar[dict | None] = ContextVar('runtime_causal', default=None)
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_ID_FIELDS = frozenset({
    'turn_id', 'batch_id', 'tool_call_id', 'task_id', 'parent_session_id',
    'related_session_id', 'span_id', 'parent_span_id',
})
_INTEGER_FIELDS = frozenset({'message_seq', 'message_index', 'stage_index'})


def _causal_ids(fields: dict) -> dict:
    """只接受固定因果标识；不把调用方任意值转为字符串。"""
    result = {}
    for key in _ID_FIELDS:
        value = fields.get(key)
        if ((isinstance(value, str) and _IDENTIFIER.fullmatch(value)) or
                (type(value) is int and 0 <= value <= 2 ** 63 - 1)):
            result[key] = value
    for key in _INTEGER_FIELDS:
        value = fields.get(key)
        if type(value) is int and 0 <= value <= 2 ** 63 - 1:
            result[key] = value
    return result


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
            return 'completed' if exit_status[1].lstrip('-0') == '' else 'error'
    return 'completed'


@contextmanager
def observation_context(**ids: Any) -> Iterator[None]:
    """在当前观测调用域中临时附加经过验证的因果标识。"""
    binding = _binding.get()
    if binding is None or not binding[0].enabled:
        yield
        return
    causal = dict(_causal.get() or {})
    causal.update(_causal_ids(ids))
    token = _causal.set(causal)
    try:
        yield
    finally:
        _causal.reset(token)


def emit_event(kind: str, **fields: Any) -> int | None:
    """向当前观测器发送元数据，不改变 Agent 行为。"""
    binding = _binding.get()
    if binding is None or not binding[0].enabled:
        return None
    return binding[0].event(kind, **fields)


@contextmanager
def operation_span(module: str, name: str = '') -> Iterator[Span]:
    """使用当前观测器创建操作边界；无观测器时只返回空句柄。"""
    binding = _binding.get()
    if binding is None:
        yield Span()
    else:
        with binding[0].span(module, name) as handle:
            yield handle


def traced(module: str):
    """为 manager 或具有 manager 属性的异步模块附加观测边界。"""
    def decorate(function):
        @wraps(function)
        async def wrapped(owner, *args, **kwargs):
            manager = getattr(owner, 'manager', owner)
            observer = getattr(manager, 'observer', None)
            if observer is None or not observer.enabled:
                return await function(owner, *args, **kwargs)
            session = args[0] if args and hasattr(args[0], 'record') else None
            session_id = session.id if session is not None else kwargs.get('session_id', '')
            turn_id = session.record.get('last_run', {}).get('turn_id') if session is not None else None
            with observer.scope(session_id, turn_id=turn_id), observer.span(module) as span:
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
    """普通函数继承当前调用域，保留原返回值和异常。"""
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with operation_span(module):
                    return await function(*args, **kwargs)
            return asynchronous
        @wraps(function)
        def synchronous(*args, **kwargs):
            with operation_span(module) as span:
                result = function(*args, **kwargs)
                if module == 'permission.confirm' and result is False:
                    span.outcome = 'rejected'
                return result
        return synchronous
    return decorate


# 保留适配器旧导入名称；Core 运行逻辑仅使用上面的中立名称。
debug_context = observation_context
debug_event = emit_event
debug_span = operation_span
