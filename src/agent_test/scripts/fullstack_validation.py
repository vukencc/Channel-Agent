"""真实模型前后端综合验收入口；业务产物只能由真实 Agent 生成。"""

import argparse
import asyncio
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import time
import traceback

from agent_test.scripts.real_multiagent_validation import (
    atomic_json,
    isolate_experiment,
    summarize_runs,
    summarize_trace,
    validate_new_root,
)


def experiment_settings(settings, root: Path, *, retained: bool = False) -> None:
    """保留模型、费用和用户配置，只覆盖本次明确授权的运行环境。"""
    if not retained:
        isolate_experiment(settings, root)
    else:
        for name, relative in (("AUDIT_LOG", "audit.jsonl"), ("RAG_CACHE_DIR", "rag-cache"),
                               ("AGENT_STATE_DIR", "state"), ("SANDBOX_DIR", "work"),
                               ("DOC_DIR", "knowledge")):
            target = root / relative
            if target.is_symlink():
                raise ValueError("resume 运行目录不能是符号链接")
            setattr(settings, name, target)
            if name != "AUDIT_LOG":
                target.mkdir(mode=0o700, exist_ok=True)
        settings.TOOL_ROOTS = {}
        settings.RAG_SOURCES = {}
        settings.DEBUG = True
        settings.DEBUG_TRACE_MAX_EVENTS = 10000
        settings.ENABLE_AGENT_TASKS = True
        settings.MAX_TOOL_ROUNDS = 0
    settings.ENABLE_COMMAND_JOBS = True
    settings.COMMAND_JOB_CONCURRENCY = 4
    settings.ENABLE_ENVIRONMENT_PREPARE = True
    settings.ENABLE_SESSION_BUDGETS = True
    settings.COMMAND_EXECUTION_ENVIRONMENT = "local"
    settings.LOCAL_COMMAND_TIMEOUT = 900.0


def validate_retained_root(path: Path, task_text: str, port: int) -> tuple[Path, dict]:
    """恢复仅允许本脚本的同一任务、同一根会话和原始临时目录。"""
    if (path.is_symlink() or not re.fullmatch(r"agent-real-multi-validation-[0-9a-f]{32}", path.name)
            or path.parent.resolve() != Path(tempfile.gettempdir()).resolve()):
        raise ValueError("resume root 不是有效的系统临时实验目录")
    root = path.resolve(strict=True)
    for name in ("state", "work"):
        directory = root / name
        if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
            raise ValueError("resume 状态或工作区目录无效")
    task, connection = root / "task.txt", root / "connection.json"
    if (task.is_symlink() or connection.is_symlink() or not task.is_file()
            or not connection.is_file() or task.read_text(encoding="utf-8") != task_text):
        raise ValueError("resume 必须匹配原始任务")
    metadata = json.loads(connection.read_text(encoding="utf-8"))
    identifier = metadata.get("parent_id", "")
    if (metadata.get("root") != str(root) or metadata.get("port") != port
            or not re.fullmatch(r"[0-9a-f]{32}", identifier)
            or metadata.get("project") != str(root / "work" / identifier)):
        raise ValueError("resume 元数据不匹配")
    for directory in (root / "state" / identifier, root / "work" / identifier):
        if directory.is_symlink() or directory.resolve(strict=True) != directory:
            raise ValueError("resume 会话与项目目录不能重定向")
    record = root / "state" / identifier / "session.json"
    if record.is_symlink() or not record.is_file():
        raise ValueError("resume 根会话不存在")
    return root, metadata


def numeric_observation(manager, started: float, *, timed_out: bool,
                        supervisor_events: list[dict]) -> dict:
    """轮询文件只暴露计数，不复制消息、计划、模型地址或错误正文。"""
    sessions = list(manager.sessions.values())
    usages = [summarize_runs(session.record) for session in sessions]
    trace = manager.debug.snapshot()
    counters = summarize_trace(trace["timeline"])
    numeric_keys = ("rounds", "model_calls", "provider_usage_calls", "input_tokens",
                    "output_tokens", "estimated_cost_usd", "unknown_cost_calls",
                    "tool_calls", "execution_progress_calls", "tool_no_progress_calls")
    return {
        "version": 1,
        "elapsed_s": round(time.monotonic() - started, 3),
        "timed_out": int(timed_out),
        "sessions": len(sessions),
        "children": sum(bool(session.record.get("delegated_from")) for session in sessions),
        "busy": sum(session.busy for session in sessions),
        "errors": sum(session.record.get("status") == "error" for session in sessions),
        "usage": {key: sum(usage[key] for usage in usages) for key in numeric_keys},
        "trace": {key: counters[key] for key in ("events_retained", "messages_sent",
                                                    "messages_read", "compactions")},
        "supervisor_calls": len(supervisor_events),
        "supervisor_failures": sum(row.get("outcome") not in {None, "completed"}
                                    for row in supervisor_events),
    }


def runtime_report(manager, root: Path, parent, started: float, *, timed_out: bool,
                   supervisor_events: list[dict]) -> dict:
    """完成状态只描述运行时，不冒充生成项目已通过独立验收。"""
    report = numeric_observation(manager, started, timed_out=timed_out,
                                 supervisor_events=supervisor_events)
    report.update({
        "terminal": True,
        "runtime_completed": not timed_out and all(
            not session.busy and session.record.get("status") == "idle"
            for session in manager.sessions.values()),
        "project_acceptance_performed": False,
        "parent_id": parent.id,
        "project": str(manager.workspace(parent)),
        "trace_summary": summarize_trace(manager.debug.snapshot()["timeline"]),
        "agents": [{"id": session.id,
                    "parent_id": session.record.get("delegated_from"),
                    "status": session.record.get("status"),
                    "last_stop_reason": session.record.get("last_run", {}).get("stop_reason"),
                    "usage": summarize_runs(session.record)}
                   for session in manager.sessions.values()],
        "supervisor": list(supervisor_events),
    })
    atomic_json(root / "trace.json", manager.debug.snapshot())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18870)
    parser.add_argument("--max-seconds", type=int, default=2400)
    parser.add_argument("--resume", action="store_true", help="修复基础设施后恢复同一任务，保留原始总墙钟预算")
    parser.add_argument("--resume-instruction-file", type=Path, help="仅恢复时使用的独立验收反馈；不替换原始任务")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 60 <= args.max_seconds <= 2400:
        parser.error("port 须为 1..65535；max-seconds 须为 60..2400")
    try:
        if args.task_file.is_symlink():
            raise ValueError("task-file 不能是符号链接")
        task_file = args.task_file.resolve(strict=True)
        if not task_file.is_file() or not 1 <= task_file.stat().st_size <= 200000:
            raise ValueError("task-file 须为 1..200000 字节的普通文件")
        task_text = task_file.read_text(encoding="utf-8")
        if not task_text.strip():
            raise ValueError("task-file 不能为空")
        resume_instruction = None
        if args.resume_instruction_file is not None:
            note = args.resume_instruction_file
            if (not args.resume or note.is_symlink() or not note.is_file()
                    or not 1 <= note.stat().st_size <= 20000):
                raise ValueError("resume-instruction-file 仅在恢复时接受 1..20000 字节的普通文件")
            resume_instruction = note.read_text(encoding="utf-8")
            if not resume_instruction.strip():
                raise ValueError("resume-instruction-file 不能为空")
        retained = None
        if args.resume:
            root, retained = validate_retained_root(args.root, task_text, args.port)
            if args.max_seconds > retained.get("max_seconds", 0):
                raise ValueError("resume 不能增加首次运行的总墙钟预算")
        else:
            root = validate_new_root(args.root)
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", args.port))
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))

    if not args.resume:
        root.mkdir(mode=0o700)
        (root / "task.txt").write_text(task_text, encoding="utf-8")
    else:
        preserved = Path(tempfile.mkdtemp(
            prefix="resume-evidence-" + str(retained.get("resumes", 0) + 1) + "-", dir=root))
        for name in ("connection.json", "observation.json", "report.json", "trace.json", "harness-error.json"):
            source = root / name
            if source.is_file() and not source.is_symlink():
                shutil.copyfile(source, preserved / name)
    console = sys.stdout
    exit_code = 0
    with (root / "runtime.log").open("a" if args.resume else "w", encoding="utf-8") as private_log:
        with redirect_stdout(private_log), redirect_stderr(private_log):
            try:
                from agent_core import config
                experiment_settings(config, root, retained=args.resume)
                config.validate_runtime_config()
                from agent_core.core.llm import call_model, close_clients
                from agent_core.core.storage import SessionStore
                from agent_frontend.web.app import create_app
                import uvicorn

                store = SessionStore(root / "state", root / "work")
                app = create_app(store=store, model=call_model)
                original = app.router.lifespan_context
                supervisor_events: list[dict] = []

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
                            status = getattr(exc, "status_code", None)
                            if type(status) is int:
                                event["http_status"] = status
                            known = {
                                "监督输入超过 SUPERVISOR_MAX_INPUT_CHARS": "input_character_limit",
                                "监督器不得调用任何工具": "unexpected_tool_call",
                                "监督回复未完成或被截断，不能用于评分或摘要": "incomplete_response",
                                "监督输出为空或超限": "empty_or_oversize_response",
                                "监督评分与历史块数量不匹配": "score_count_mismatch",
                                "监督评分数量不匹配": "score_count_mismatch",
                            }
                            event["failure_category"] = known.get(str(exc), {
                                "JSONDecodeError": "invalid_json", "ValidationError": "invalid_schema",
                                "TimeoutError": "timeout", "APITimeoutError": "request_timeout",
                                "CancelledError": "cancelled_or_shared_deadline",
                            }.get(type(exc).__name__, "unclassified"))
                            raise
                        finally:
                            event["elapsed_s"] = round(time.monotonic() - began, 3)

                    async def inspect(self, snapshot):
                        return await self._observe("inspect", lambda: self.delegate.inspect(snapshot))

                    async def summarize(self, previous_summary, messages, *, kind="session"):
                        return await self._observe(kind, lambda: self.delegate.summarize(
                            previous_summary, messages, kind=kind))

                    async def close(self):
                        await self.delegate.close()

                @asynccontextmanager
                async def lifespan(current):
                    try:
                        async with original(current):
                            manager = current.state.manager
                            if manager.history_context:
                                manager.history_context.supervisor = ObservedSupervisor(
                                    manager.history_context.supervisor)
                            if retained is None:
                                parent = manager.create(title="团队资产借用管理系统真实验收",
                                                        execution_environment="local")
                            else:
                                parent = manager.sessions[retained["parent_id"]]
                                if parent.record.get("delegated_from") or parent.busy:
                                    raise ValueError("resume 目标必须是已停止的根会话")
                                manager.set_execution_environment(parent, "local")
                            manager.set_permission_policy(parent, "full_access")
                            project = manager.workspace(parent)
                            started_epoch = (retained.get("started_epoch", (root / "task.txt").stat().st_mtime)
                                             if retained else time.time())
                            started = time.monotonic() - max(0, time.time() - started_epoch)
                            if time.monotonic() - started >= args.max_seconds:
                                raise ValueError("原始任务的总墙钟预算已经耗尽")
                            timed_out = False
                            atomic_json(root / "connection.json", {
                                "version": 1, "root": str(root), "project": str(project),
                                "parent_id": parent.id, "port": args.port,
                                "url": f"http://127.0.0.1:{args.port}",
                                "debug_url": f"http://127.0.0.1:{args.port}/debug?session={parent.id}",
                                "max_seconds": args.max_seconds,
                                "started_epoch": started_epoch,
                                "resumes": retained.get("resumes", 0) + 1 if retained else 0,
                                "pid": os.getpid(),
                            })
                            default_resume = (
                                "继续完成本会话最初的团队资产借用管理系统任务，保留已有计划、代码与所有真实验证证据。"
                                "上一轮因本机执行实现误过滤宿主标准网络代理变量而由验收器有序停止；"
                                "现已修复标准代理继承，工具进程重新启动，可重新检测官方源连接并继续项目内工具链准备。"
                                "这不是新任务，也不要重置现有业务产物或降低验收要求；仍须至少两个前后端子Agent。"
                                "总墙钟2400秒从首次启动计算，优先并行推进和真实编译测试，完成后停止服务释放端口。")
                            continuation = (("继续本会话原始任务，保留已有项目、计划和真实执行证据。"
                                "下面是独立验收反馈，请核查后修复并实际重测；不能把失败说成成功。\n\n"
                                + resume_instruction) if resume_instruction else default_resume)
                            manager.submit(parent, task_text if retained is None else continuation)

                            async def monitor():
                                nonlocal timed_out
                                while True:
                                    if not timed_out and time.monotonic() - started >= args.max_seconds:
                                        timed_out = True
                                        for value in list(manager.sessions.values()):
                                            if value.busy:
                                                manager.cancel(value)
                                    atomic_json(root / "observation.json", numeric_observation(
                                        manager, started, timed_out=timed_out,
                                        supervisor_events=supervisor_events))
                                    if not any(session.busy for session in manager.sessions.values()):
                                        await manager.flush()
                                        report = runtime_report(manager, root, parent, started,
                                            timed_out=timed_out, supervisor_events=supervisor_events)
                                        atomic_json(root / "report.json", report)
                                        print(json.dumps({"report": str(root / "report.json"),
                                            "runtime_completed": report["runtime_completed"],
                                            "timed_out": timed_out,
                                            "project_acceptance_performed": False}),
                                            file=console, flush=True)
                                        return
                                    await asyncio.sleep(1)

                            worker = asyncio.create_task(monitor())
                            try:
                                yield
                            finally:
                                worker.cancel()
                                await asyncio.gather(worker, return_exceptions=True)
                                atomic_json(root / "observation.json", numeric_observation(
                                    manager, started, timed_out=timed_out,
                                    supervisor_events=supervisor_events))
                    finally:
                        store.close()
                        await close_clients()

                app.router.lifespan_context = lifespan
                uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False,
                            log_level="warning", timeout_graceful_shutdown=5)
            except BaseException as exc:
                traceback.print_exc(file=private_log)
                atomic_json(root / "harness-error.json", {"error_type": type(exc).__name__})
                print(json.dumps({"report": str(root / "harness-error.json"),
                                  "error_type": type(exc).__name__}), file=console, flush=True)
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
