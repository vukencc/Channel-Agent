"""只读投影当前 Flow 实例的运行元数据，不读取会话正文或项目文件。"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

from agent_core import config

if TYPE_CHECKING:
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import Session


ACTIVE_RUN_STATES = {"queued", "running", "cancelling"}
ACTIVE_CHILD_STATES = {"queued", "running", "confirming", "stopping"}
MAX_VISIBLE_CHILDREN = 40
MAX_TASK_DETAIL_CHARS = 4000
MAX_TASK_SUMMARY_CHARS = 160


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _task_preview(row: dict) -> None:
    task = row["task"]
    row["task_summary"] = _clip(" ".join(task.split()), MAX_TASK_SUMMARY_CHARS)
    row["task"] = _clip(task, MAX_TASK_DETAIL_CHARS)
    if row["phase"]:
        row["phase"] = _clip(row["phase"], MAX_TASK_SUMMARY_CHARS)


def _model_details(metrics: dict, configured: str) -> tuple[str, int, int]:
    calls = metrics.get("model_calls", [])
    last = calls[-1] if calls and isinstance(calls[-1], dict) else {}
    model = last.get("model")
    return (model if isinstance(model, str) and model else configured,
            len(calls), len(metrics.get("tools", [])))


def _runtime_status(status: str, session: "Session") -> str:
    """活动节点或委派任务显示会话的排队、确认和停止子状态。"""
    current = session.record["status"]
    return current if status == "running" and current in {"queued", "confirming", "stopping"} else status


def _children(service: "FlowService", session: "Session", run_id: str,
              model_for: Callable[[str | None], str]) -> tuple[list[dict], int, int]:
    queue = service.manager.agent_tasks
    if queue is None:
        return [], 0, 0
    active, ended = [], []
    for task_id in session.flow_task_ids:
        task = queue.records.get(task_id)
        if task is None or task.get("owner") != session.id:
            continue
        child = service.manager.sessions.get(task.get("child_id"))
        if child is None or (child.record.get("delegated_from"), child.record.get("flow_run_id")) != (session.id, run_id):
            continue
        last_run = child.record.get("last_run", {})
        matching_turn = bool(task.get("turn_id") and task["turn_id"] == last_run.get("turn_id"))
        status = task["status"]
        if matching_turn:
            if status == "cancelled" and child.turn_active and child.record["status"] == "stopping":
                status = "stopping"
            else:
                status = _runtime_status(status, child)
        (active if status in ACTIVE_CHILD_STATES else ended).append((task, child, status, last_run if matching_turn else {}))
    visible = active[:MAX_VISIBLE_CHILDREN]
    remaining = MAX_VISIBLE_CHILDREN - len(visible)
    if remaining:
        visible.extend(ended[-remaining:])
    rows = []
    for task, child, status, turn in visible:
        model, _, _ = _model_details(turn, task.get("model") or model_for(child.record.get("model_profile")))
        rows.append({
            "id": child.id, "title": child.record["title"], "status": status,
            "task": task["task"], "model": model, "phase": (child.phase or None) if turn else None,
            "tools": service.manager.session_tools(child),
        })
    return rows, len(active) + len(ended), len(active)


def working_runs(service: "FlowService", *,
                 redact: Callable[[list[dict]], list[dict]] | None = None) -> list[dict]:
    """使用正在调度的实例与当前轮身份，避免复用会话时显示上一轮数据。"""
    models: dict[str | None, str] = {}
    runtime_profiles: set[str | None] = set()

    def model_for(profile: str | None) -> str:
        if profile not in models:
            try:
                endpoint = service.manager.model_registry.resolve(profile)
            except ValueError:
                endpoint = None
            if endpoint:
                runtime_profiles.add(profile)
            models[profile] = endpoint.model if endpoint else config.MODEL_PROFILES.get(profile, {}).get("model") or config.MODEL
        return models[profile]

    rows = []
    for flow_id, task in service.tasks.items():
        if task.done():
            continue
        value = service.records[flow_id]
        run = value.get("run")
        if not run or run["status"] not in ACTIVE_RUN_STATES:
            continue
        project_id = value["project_id"]
        project = service.projects.records.get(project_id, {}) if project_id and service.projects else {}
        definitions = {node["id"]: node for node in value["nodes"]}
        settings = run.get("execution_settings", {})
        nodes = []
        for node_id, result in run["nodes"].items():
            node = definitions[node_id]
            profile = node["model_profile"] or settings.get("model_profile", value["model_profile"])
            session_id = result["session_id"]
            session = service.manager.sessions.get(session_id)
            status = result["status"]
            phase = None
            metrics = {}
            subagents, total_children, active_children = [], 0, 0
            if session is not None and (session.record.get("flow_id"), session.record.get("flow_node_id"),
                                        session.record.get("flow_run_id")) == (flow_id, node_id, run["id"]):
                profile = session.record.get("model_profile", profile)
                last_run = session.record.get("last_run", {})
                if result.get("turn_id") and result["turn_id"] == last_run.get("turn_id"):
                    metrics = last_run
                    phase = session.phase or None
                    status = _runtime_status(status, session)
                subagents, total_children, active_children = _children(service, session, run["id"], model_for)
                tools = service.manager.session_tools(session)
            else:
                # 尚未启动的节点也按相同的部署目录和节点配置计算可调用工具。
                from agent_core.core.flow_templates import REQUIRED_TOOL_NAMES
                configured_names = node.get("tool_names")
                preview = SimpleNamespace(id="working-" + node_id, record={
                    "flow_id": flow_id, "custom_tools": node.get("custom_tools", []),
                    "tool_names": (None if configured_names is None else
                                   list(dict.fromkeys([*configured_names, *REQUIRED_TOOL_NAMES]))),
                })
                tools = service.manager.session_tools(preview)
            model, model_calls, tool_calls = _model_details(metrics, model_for(profile))
            thinking_mode = node["thinking_mode"] or (None if profile in runtime_profiles else config.THINKING_MODE)
            effort = node["reasoning_effort"] or (None if profile in runtime_profiles else config.REASONING_EFFORT)
            nodes.append({
                "id": node_id, "title": node["title"], "status": status, "session_id": session_id,
                "task": node["prompt"], "model": model, "model_profile": profile, "phase": phase,
                "thinking_mode": thinking_mode, "reasoning_effort": None if thinking_mode == "disabled" else effort,
                "started_at": result.get("started_at"), "subagent_count": total_children,
                "active_subagent_count": active_children, "subagents": subagents,
                "handoff_validation": _copy_validation(result.get("handoff_validation")),
                "model_calls": model_calls, "tool_calls": tool_calls,
                "tools": tools,
            })
        rows.append({
            "run_id": run["id"], "flow_id": flow_id, "project_id": project_id,
            "title": project.get("title", value["title"]), "status": run["status"], "started_at": run["started_at"],
            "current_node": run["current_node"], "nodes": nodes, "edges": [dict(edge) for edge in value["edges"]],
        })
    # 先隐藏已知凭据，再截断概要，避免从截断边界泄露部分凭据。
    if redact is not None and rows:
        rows = redact(rows)
    for row in rows:
        for node in row["nodes"]:
            _task_preview(node)
            for child in node["subagents"]:
                _task_preview(child)
    return rows


def _copy_validation(validation: dict | None) -> dict | None:
    if validation is None:
        return None
    return {key: validation[key] for key in ("status", "code", "message") if key in validation}
