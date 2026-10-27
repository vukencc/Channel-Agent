"""Run one real-model, isolated multi-agent WebUI experiment and retain evidence."""

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import time


ROOT_NAME = re.compile(r"agent-real-multi-validation-[0-9a-f]{32}\Z")
MODEL_KEYS = {"input_tokens", "output_tokens", "cached_input_tokens", "tokens_source",
              "estimated_cost_usd", "total_s", "outcome", "error_type", "finish_reason"}
TOOL_KEYS = {"name", "source", "wall_s_including_confirmation", "output_chars", "error_type"}


def validate_new_root(path: Path, *, temp_root: Path | None = None) -> Path:
    """Reject reused, linked, or non-system-temp experiment directories."""
    temp = (temp_root or Path(tempfile.gettempdir())).resolve(strict=True)
    if not ROOT_NAME.fullmatch(path.name) or path.is_symlink() or path.exists():
        raise ValueError("--root must be a new agent-real-multi-validation-<uuidhex> directory")
    if path.parent.resolve() != temp:
        raise ValueError("--root must be directly inside the system temporary directory")
    return temp / path.name


def isolate_experiment(settings, root: Path) -> None:
    """Redirect every persistent application root before importing runtime modules."""
    settings.AUDIT_LOG = root / "audit.jsonl"
    settings.RAG_CACHE_DIR = root / "rag-cache"
    settings.AGENT_STATE_DIR = root / "state"
    settings.SANDBOX_DIR = root / "work"
    settings.DOC_DIR = root / "knowledge"
    settings.TOOL_ROOTS = {}
    settings.RAG_SOURCES = {}
    settings.DEBUG = True
    settings.DEBUG_TRACE_MAX_EVENTS = 10000
    settings.ENABLE_AGENT_TASKS = True
    settings.ENABLE_SESSION_BUDGETS = True
    settings.MAX_TOOL_ROUNDS = 0
    for name in ("RAG_CACHE_DIR", "AGENT_STATE_DIR", "SANDBOX_DIR", "DOC_DIR"):
        target = getattr(settings, name)
        if target.is_symlink():
            raise ValueError("experiment path cannot be a symlink")
        target.mkdir(mode=0o700)


def _safe_number(value):
    return value if type(value) in (int, float) and value >= 0 else None


def summarize_runs(record: dict) -> dict:
    """Aggregate retained real usage without copying messages or credentials."""
    runs_by_id = {run["turn_id"]: run for run in record.get("run_history", [])
                  if isinstance(run, dict) and run.get("turn_id")}
    latest = record.get("last_run", {})
    if isinstance(latest, dict) and latest.get("turn_id"):
        runs_by_id[latest["turn_id"]] = latest
    runs = list(runs_by_id.values())
    calls = [call for run in runs for call in run.get("model_calls", []) if isinstance(call, dict)]
    tools = [tool for run in runs for tool in run.get("tools", []) if isinstance(tool, dict)]
    return {
        "rounds": len([run for run in runs if run.get("turn_id")]),
        "model_calls": len(calls),
        "provider_usage_calls": sum(call.get("tokens_source") == "provider" for call in calls),
        "input_tokens": sum(_safe_number(call.get("input_tokens")) or 0 for call in calls),
        "output_tokens": sum(_safe_number(call.get("output_tokens")) or 0 for call in calls),
        "estimated_cost_usd": sum(_safe_number(call.get("estimated_cost_usd")) or 0 for call in calls),
        "unknown_cost_calls": sum(_safe_number(call.get("estimated_cost_usd")) is None for call in calls),
        "model_outcomes": dict(Counter(str(call.get("outcome", "unknown")) for call in calls)),
        "tool_calls": len(tools),
        "execution_progress_calls": sum(tool.get("execution_progress") is True for tool in tools),
        "tool_no_progress_calls": sum(tool.get("execution_progress") is False for tool in tools),
        "tool_sources": dict(Counter(str(tool.get("source", "unknown")) for tool in tools)),
        "model_metrics": [{key: call[key] for key in MODEL_KEYS if key in call} for call in calls],
        "tool_metrics": [{key: tool[key] for key in TOOL_KEYS if key in tool} for tool in tools],
    }


def summarize_trace(timeline: list[dict]) -> dict:
    counts = Counter(row.get("kind") for row in timeline)
    return {"events_retained": len(timeline), "event_counts": dict(counts),
            "messages_sent": counts["message.sent"], "messages_read": counts["message.read"],
            "compactions": counts["context.compacted"],
            "tool_outcomes": dict(Counter(row.get("state", "unknown") for row in timeline
                                          if row.get("kind") == "tool.completed")),
            "terminal_reasons": dict(Counter(row.get("reason", "unknown") for row in timeline
                                             if row.get("kind") == "turn.stopped"))}


def atomic_json(path: Path, data: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".observation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def collect(manager, root: Path, supervisor_events: list[dict], started: float,
            timed_out: bool = False) -> dict:
    from agent_frontend.web.context_status import context_status
    trace = manager.debug.snapshot()
    sessions = []
    for session in manager.sessions.values():
        record = session.record
        archive = root / "state" / session.id / "context" / "manifest.json"
        revision = None
        chunks = None
        if archive.is_file() and not archive.is_symlink():
            try:
                manifest = json.loads(archive.read_text(encoding="utf-8"))
                revision = manifest.get("revision")
                chunks = len(manifest.get("chunks", []))
            except (OSError, ValueError, TypeError):
                pass
        sessions.append({"id": session.id, "parent_id": record.get("delegated_from"),
                         "status": record.get("status"), "busy": session.busy,
                         "phase": session.phase, "message_count": len(record["messages"]),
                         "last_stop_reason": record.get("last_run", {}).get("stop_reason"),
                         "error_type": (match.group(1) if record.get("status") == "error" and
                                        (match := re.match(r"([A-Za-z_][A-Za-z0-9_]{0,79}):",
                                                           str(record.get("error", "")))) else None),
                         "context_revision": revision, "context_chunks": chunks,
                         "context_status": context_status(session, manager.history_context),
                         "plan": manager.task_plans.peek(session) if manager.task_plans else None,
                         "usage": summarize_runs(record)})
    return {"version": 1, "elapsed_s": round(time.monotonic() - started, 3),
            "timed_out": timed_out, "sessions": sessions,
            "child_count": sum(item["parent_id"] is not None for item in sessions),
            "all_sessions_runtime_completed": all(item["status"] == "idle" for item in sessions),
            "trace": summarize_trace(trace["timeline"]), "trace_snapshot": trace,
            "supervisor": {"calls": len(supervisor_events), "events": list(supervisor_events)}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8978)
    parser.add_argument("--max-seconds", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 60 <= args.max_seconds <= 7200:
        parser.error("port must be 1..65535 and max-seconds must be 60..7200")
    try:
        root = validate_new_root(args.root)
        if args.task_file.is_symlink():
            raise ValueError("--task-file cannot be a symlink")
        task_file = args.task_file.resolve(strict=True)
        if not task_file.is_file() or not 1 <= task_file.stat().st_size <= 200000:
            raise ValueError("--task-file must be a regular 1..200000 byte file")
        task_text = task_file.read_text(encoding="utf-8")
        if not task_text.strip():
            raise ValueError("--task-file cannot be blank")
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    root.mkdir(mode=0o700)
    (root / "task.txt").write_text(task_text, encoding="utf-8")

    from agent_core import config
    isolate_experiment(config, root)
    config.validate_runtime_config()
    from agent_core.core.llm import call_model, close_clients
    from agent_core.core.storage import SessionStore
    from agent_frontend.web.app import create_app
    import uvicorn

    store = SessionStore(root / "state", root / "work")
    app = create_app(store=store, model=call_model)
    original = app.router.lifespan_context
    supervisor_events = []

    class ObservedSupervisor:
        def __init__(self, delegate):
            self.delegate, self.settings = delegate, delegate.settings

        async def _observe(self, kind, callback):
            began = time.monotonic()
            event = {"kind": kind}
            supervisor_events.append(event)
            try:
                result = await callback()
                event["outcome"] = "completed"
                return result
            except BaseException as exc:
                event["outcome"] = type(exc).__name__
                raise
            finally:
                event["elapsed_s"] = round(time.monotonic() - began, 3)

        async def inspect(self, snapshot):
            return await self._observe("inspect", lambda: self.delegate.inspect(snapshot))

        async def summarize(self, previous_summary, messages, *, kind="session"):
            return await self._observe(kind, lambda: self.delegate.summarize(previous_summary, messages, kind=kind))

        async def close(self):
            await self.delegate.close()

    @asynccontextmanager
    async def lifespan(current):
        async with original(current):
            manager = current.state.manager
            if manager.history_context:
                manager.history_context.supervisor = ObservedSupervisor(manager.history_context.supervisor)
            parent = manager.create(title="Real multi-agent validation")
            manager.set_permission_policy(parent, "full_access")
            started = time.monotonic()
            manager.submit(parent, task_text)
            timed_out = False

            async def monitor():
                nonlocal timed_out
                while True:
                    if not timed_out and time.monotonic() - started >= args.max_seconds:
                        timed_out = True
                        for value in list(manager.sessions.values()):
                            if value.busy:
                                manager.cancel(value)
                    report = collect(manager, root, supervisor_events, started, timed_out)
                    atomic_json(root / "observation.json", report)
                    if not parent.busy and not any(s.busy for s in manager.sessions.values()):
                        await manager.flush()
                        report = collect(manager, root, supervisor_events, started, timed_out)
                        report["terminal"] = True
                        report["runtime_completed"] = (not timed_out and
                            report["all_sessions_runtime_completed"] and parent.record.get("status") == "idle")
                        atomic_json(root / "report.json", report)
                        print(json.dumps({"report": str(root / "report.json"),
                                          "runtime_completed": report["runtime_completed"], "timed_out": timed_out,
                                          "url": f"http://127.0.0.1:{args.port}/debug"}), flush=True)
                        return
                    await asyncio.sleep(1)

            worker = asyncio.create_task(monitor())
            print(json.dumps({"root": str(root), "parent_id": parent.id,
                              "url": f"http://127.0.0.1:{args.port}/debug",
                              "max_seconds": args.max_seconds}), flush=True)
            try:
                yield
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                atomic_json(root / "observation.json", collect(manager, root, supervisor_events, started, timed_out))
        store.close()
        await close_clients()

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False,
                log_level="warning", timeout_graceful_shutdown=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
