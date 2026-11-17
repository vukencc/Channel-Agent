"""为应用独立事件循环选择与计时器精度一致的时钟。"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Coroutine, TypeVar

T = TypeVar('T')


def use_high_resolution_clock() -> bool:
    """只调整 Windows 粗粒度时钟上的标准策略与标准循环工厂。"""
    if sys.platform != 'win32':
        return False
    if time.get_clock_info('monotonic').resolution <= time.get_clock_info('perf_counter').resolution:
        return False
    policy = asyncio.get_event_loop_policy()
    for policy_name, loop_name in (('WindowsProactorEventLoopPolicy', 'ProactorEventLoop'),
                                   ('WindowsSelectorEventLoopPolicy', 'SelectorEventLoop')):
        policy_type = getattr(asyncio, policy_name, None)
        loop_type = getattr(asyncio, loop_name, None)
        if policy_type is not None and type(policy) is policy_type:
            return getattr(policy, '_loop_factory', None) is loop_type
    return False


def new_runtime_loop() -> asyncio.AbstractEventLoop:
    if not use_high_resolution_clock():
        return asyncio.new_event_loop()
    policy = asyncio.get_event_loop_policy()
    base = policy._loop_factory

    class HighResolutionLoop(base):
        def time(self) -> float:
            return time.perf_counter()

    loop = HighResolutionLoop()
    loop._clock_resolution = time.get_clock_info('perf_counter').resolution
    return loop


def run_runtime(coro: Coroutine[Any, Any, T]) -> T:
    """只管理本次创建的循环，不更改进程全局策略或现有循环。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError('run_runtime() cannot be called from a running event loop')
    with asyncio.Runner(loop_factory=new_runtime_loop) as runner:
        return runner.run(coro)
