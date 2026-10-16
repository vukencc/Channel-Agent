"""离线测量结构化上下文压缩开销；不连接模型，也不读取项目 .env。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace


def _disable_dotenv() -> None:
    """配置模块本身调用 load_dotenv；屏蔽它，避免 benchmark 读取实际 .env。"""
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False


async def _run_once(run_no: int, *, summary_delay_ms: float, rounds: int, compact_ratio: float,
                    supervisor_mode: str) -> dict:
    _disable_dotenv()
    from ai_agent_startup import config
    from ai_agent_startup.core.history_context import HistoryContext
    from ai_agent_startup.core.sessions import SessionManager
    from ai_agent_startup.core.storage import SessionStore

    config.ENABLE_STRUCTURED_CONTEXT = False
    config.ENABLE_TASK_PLANS = False
    config.ENABLE_AGENT_TASKS = False
    config.ENABLE_COMMAND_JOBS = False
    config.CONTEXT_COMPACT_RATIO = compact_ratio
    config.CONTEXT_HISTORY_KEEP_GROUPS = 2
    config.CONTEXT_HISTORY_CHUNK_CHARS = 7000
    config.CONTEXT_SUMMARY_CHARS = 2000
    config.MODEL_INPUT_CHARS = 32000
    config.MODEL_INPUT_TOKENS = 100000

    root = Path(tempfile.mkdtemp(prefix=f"context-progress-{run_no}-"))
    store = SessionStore(root / "state", root / "work")

    async def no_model(*args, **kwargs):
        raise AssertionError("benchmark must not call the execution model")

    manager = SessionManager(store, model=no_model)
    summary_calls: list[dict] = []
    inspection_calls: list[dict] = []

    class DeterministicSummaryStub:
        enabled = supervisor_mode != "disabled"
        settings = SimpleNamespace(enabled=enabled, timeout=600, max_input_chars=12000,
                                   check_interval=1 if enabled else 100000,
                                   variance_threshold=.75, max_chunks=24)

        async def inspect(self, snapshot):
            chunk_count = len(snapshot.get("chunks", []))
            inspection_calls.append({"chunk_count": chunk_count})
            if supervisor_mode == "mixed":
                values = [float(index % 2) for index in range(chunk_count)]
            else:
                values = [.5] * chunk_count
            return {"compact": False, "reason": "synthetic benchmark score pattern", "values": values}

        async def summarize(self, previous, messages, *, kind="session"):
            summary_calls.append({"kind": kind, "input_messages": len(messages),
                                  "input_chars": len(json.dumps(messages, ensure_ascii=False))})
            if summary_delay_ms:
                await asyncio.sleep(summary_delay_ms / 1000)
            # Deterministic and explicitly non-semantic: enough content for repeated prepare.
            return (previous + f" [stub:{kind}:{len(messages)}]")[-config.CONTEXT_SUMMARY_CHARS:]

    context = HistoryContext(manager, supervisor=DeterministicSummaryStub())
    manager.history_context = context
    session = manager.create(title="SYNTHETIC offline context benchmark")
    messages = [{"role": "system", "content": "Synthetic benchmark only; no real project task."}]
    prepares = []
    try:
        # Generated, labeled trajectory: one user/assistant pair per step, no tools run.
        for index in range(rounds):
            messages.append({"role": "user", "content":
                f"SYNTHETIC step={index:03d}; investigate a fictional module and preserve its constraints. "
                + (f"detail-{index:03d} " * 110)})
            messages.append({"role": "assistant", "content":
                f"SYNTHETIC observation step={index:03d}; no files were read or changed. "
                + (f"finding-{index:03d} " * 75)})
            # Latest user message is the active request, matching HistoryContext's selection rule.
            messages.append({"role": "user", "content":
                f"SYNTHETIC continue step={index:03d}; summarize progress without executing tools."})
            started = time.perf_counter()
            _, metrics = await context.prepare(session, messages, schemas=[])
            prepares.append({"elapsed_s": time.perf_counter() - started,
                             "compact_reason": metrics.get("compact_reason", ""),
                             "revision": metrics.get("context_revision", 0),
                             "utilization": metrics.get("utilization", 0),
                "archived_chunks": metrics.get("archived_chunks", 0),
                             "supervisor_status": metrics.get("supervisor", ""),
                             "supervisor_value_variance": metrics.get("value_variance")})
        await manager.shutdown()
        return {"prepare_count": len(prepares), "compact_prepare_count": sum(bool(x["compact_reason"]) for x in prepares),
                "final_revision": prepares[-1]["revision"],
                "summary_calls": len(summary_calls),
                "inspection_calls": len(inspection_calls),
                "prepare_compact_reasons": {reason: sum(x["compact_reason"] == reason for x in prepares)
                                            for reason in sorted({x["compact_reason"] for x in prepares})},
                "prepare_supervisor_statuses": {status: sum(x["supervisor_status"] == status for x in prepares)
                                                for status in sorted({x["supervisor_status"] for x in prepares})},
                "prepare_trace": prepares,
                "supervisor_mode": supervisor_mode,
                "summary_calls_by_kind": {kind: sum(c["kind"] == kind for c in summary_calls)
                                          for kind in sorted({c["kind"] for c in summary_calls})},
                "summary_pages_input_chars": sum(c["input_chars"] for c in summary_calls),
                "prepare_elapsed_s": sum(x["elapsed_s"] for x in prepares),
                "prepare_median_ms": statistics.median(x["elapsed_s"] for x in prepares) * 1000,
                "final_utilization": prepares[-1]["utilization"],
                "max_archived_chunks": max(x["archived_chunks"] for x in prepares),
                "input_kind": "SYNTHETIC generated trajectory; no tools/files/model calls",
                "summary_stub_delay_ms_per_call": summary_delay_ms}
    finally:
        if not manager.closing:
            await manager.shutdown()
        store.close()


def _observation_report(paths: list[Path]) -> list[dict]:
    """只计算 observation 元数据，不输出消息正文或会话标识。"""
    reports = []
    for path in paths:
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        sessions = data.get("sessions", [])
        events = data.get("supervisor_events", [])
        report = {"file": path.name, "sessions": len(sessions),
                  "supervisor_events": len(events),
                  "supervisor_elapsed_s": round(sum(float(e.get("elapsed_s", 0)) for e in events), 3),
                  "supervisor_status_counts": {}, "model_total_s": 0.0,
                  "tool_call_count": 0, "repeated_read_file_calls": 0,
                  "plan_revision": None, "plan_status": None,
                  "context_compact_reason": None, "context_utilization": None,
                  "context_revision": None, "context_archived_chunks": None}
        for event in events:
            status = str(event.get("status", "unknown"))
            report["supervisor_status_counts"][status] = report["supervisor_status_counts"].get(status, 0) + 1
        for session in sessions:
            run = session.get("last_run", {})
            report["model_total_s"] += sum(float(call.get("total_s", 0)) for call in run.get("model_calls", []))
            names = session.get("tool_names", [])
            report["tool_call_count"] += len(names)
            reads = sum(name == "read_file" for name in names)
            report["repeated_read_file_calls"] += max(0, reads - 1)
            plan = session.get("plan") or {}
            report["plan_revision"] = plan.get("revision", report["plan_revision"])
            report["plan_status"] = plan.get("status", report["plan_status"])
            context = run.get("context", {})
            for key, source in (("context_compact_reason", "compact_reason"),
                                ("context_utilization", "utilization"),
                                ("context_revision", "context_revision"),
                                ("context_archived_chunks", "archived_chunks")):
                report[key] = context.get(source, report[key])
        report["model_total_s"] = round(report["model_total_s"], 3)
        reports.append(report)
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=36)
    parser.add_argument("--compact-ratio", type=float, default=.8, help="对比阈值，默认维持基线 .8")
    parser.add_argument("--summary-delay-ms", type=float, default=0,
                        help="每次 stub 摘要调用人为延迟；不是 LLM 延迟，默认 0")
    parser.add_argument("--supervisor-mode", choices=("disabled", "uniform", "mixed"), default="disabled",
                        help="禁用检查，或使用固定同分/交替 0..1 合成评分")
    parser.add_argument("--output", type=Path,
                        default=Path(tempfile.gettempdir()) / "context-progress-benchmark.json")
    parser.add_argument("--observations", nargs="*", type=Path, default=[
        Path("/tmp/agent-project-validation-engineering-20261007/observation.json"),
        Path("/tmp/agent-project-validation-engineering-20261007/observation-before-resume.json")])
    args = parser.parse_args()
    if args.runs < 1 or args.rounds < 2 or args.summary_delay_ms < 0 or not .1 <= args.compact_ratio <= .95:
        parser.error("runs/rounds 必须为正数，summary delay 不得为负")
    async def measure():
        return [await _run_once(i, summary_delay_ms=args.summary_delay_ms, rounds=args.rounds,
                                compact_ratio=args.compact_ratio, supervisor_mode=args.supervisor_mode)
                for i in range(args.runs)]
    samples = asyncio.run(measure())
    report = {"method": "real SessionManager + HistoryContext.prepare with deterministic summary stub",
              "environment": {"python": sys.version.split()[0], "platform": sys.platform,
                              "runs": args.runs, "rounds": args.rounds,
                              "summary_delay_ms_per_call": args.summary_delay_ms, "compact_ratio": args.compact_ratio,
                              "supervisor_mode": args.supervisor_mode},
              "samples": samples,
              "summary": {"prepare_elapsed_median_s": statistics.median(x["prepare_elapsed_s"] for x in samples),
                          "prepare_elapsed_range_s": [min(x["prepare_elapsed_s"] for x in samples),
                                                       max(x["prepare_elapsed_s"] for x in samples)],
                          "summary_calls_median": statistics.median(x["summary_calls"] for x in samples),
                          "compact_prepares_median": statistics.median(x["compact_prepare_count"] for x in samples),
                          "inspection_calls_median": statistics.median(x["inspection_calls"] for x in samples)},
              "observations": _observation_report(args.observations),
              "limitations": ["Stub摘要不理解语义、不调用LLM；人为延迟只是控制变量。",
                              "合成轨迹不等于真实任务回放；benchmark耗时不等于端到端 supervisor/model 时间。",
                              "observation汇总只反映所给快照，不能推断停滞原因的因果关系。"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
