"""组合根：只在显式启用诊断时加载可选 AgentTest 适配器。"""
from agent_core import config
from agent_core.core.instrumentation import NullObserver, RuntimeObserver


def create_observer() -> RuntimeObserver:
    """普通运行只使用 Core 的空观察器，不导入诊断实现。"""
    if not config.DEBUG:
        return NullObserver()
    from agent_test.runtime import DebugRecorder
    return DebugRecorder(enabled=True, capacity=config.DEBUG_TRACE_MAX_EVENTS)


def configure_debug_tools() -> None:
    """在创建界面前注册可选工具，并保持已有 schema 列表引用有效。"""
    if not config.ENABLE_DEBUG_TOOL:
        return
    from agent_test.tool import register_debug_tool
    from agent_core.tools import all_schemas
    from agent_core.core.llm import TOOL_SCHEMAS
    register_debug_tool()
    TOOL_SCHEMAS[:] = all_schemas()


def render_debug(manager) -> list[tuple[str, str]]:
    if not manager.observer.enabled:
        return []
    from agent_test.cli import render_debug as render
    return render(manager)


def install_debug_routes(app, **services) -> frozenset[str]:
    """宿主注入可信会话、异步 IO 与状态读取，诊断模块不依赖界面。"""
    if not config.DEBUG:
        return frozenset()
    from agent_test.web.router import DebugWebServices, SNAPSHOT_PATHS, create_router
    app.include_router(create_router(DebugWebServices(**services)))
    return SNAPSHOT_PATHS
