#!/usr/bin/env python3
"""Offline, privacy-safe measurements for a saved structured-context session.

This tool reads the transcript, audit and context archive, but never replays tool
calls or contacts a model endpoint. It reports only aggregate lengths and counts.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import copy
import hashlib
import importlib.util
import json
import platform
from pathlib import Path
import statistics
import subprocess
import shlex
import threading
import time
from types import MethodType, SimpleNamespace


DEFAULT_SESSION = ".agent/274f7c332e414726a51ce4fef5c3fffd"
DEFAULT_REPORT = ".cache/reports/perf/compact_trace_replay.md"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def iso_time(row):
    value = row.get("created_at") or row.get("ts")
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def schema_measurements(repeats: int):
    # Imports are deliberately inside the offline benchmark; no client/API code is called.
    from ai_agent_startup import config
    from ai_agent_startup.core.context import estimate_tokens
    from ai_agent_startup.core.tool_settings import bounded_schemas, filter_schemas
    from ai_agent_startup.tools import all_schemas

    registered = all_schemas()
    before = filter_schemas(registered, config.MODEL_TOOL_NAMES)
    after = bounded_schemas(registered, config.MODEL_TOOL_NAMES)

    def measure(value):
        encoded = json.dumps(value, ensure_ascii=False)
        return {
            "tools": len(value),
            "json_chars": len(encoded),
            "estimated_tokens": estimate_tokens(encoded),
        }

    # Count only JSON-schema annotation titles. Property names such as `title`,
    # and literal values in defaults/enums, are business data and are preserved.
    def count_schema_titles(parameters):
        count = 0

        def visit(node):
            nonlocal count
            if not isinstance(node, dict):
                return
            count += "title" in node
            for key in (
                "properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
            ):
                children = node.get(key)
                if isinstance(children, dict):
                    for child in children.values():
                        visit(child)
            for key in (
                "items", "additionalProperties", "unevaluatedProperties", "contains", "not",
                "if", "then", "else", "propertyNames", "additionalItems",
            ):
                child = node.get(key)
                if isinstance(child, list):
                    for item in child:
                        visit(item)
                else:
                    visit(child)
            for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
                children = node.get(key)
                if isinstance(children, list):
                    for child in children:
                        visit(child)

        visit(parameters)
        return count

    titles_before = sum(count_schema_titles(row["function"].get("parameters", {})) for row in before)
    titles_after = sum(count_schema_titles(row["function"].get("parameters", {})) for row in after)

    # A paired, repeated local serialization measurement using identical schemas.
    # This captures the client payload preparation cost only, not network/model time.
    elapsed = {"before": [], "after": []}
    for _ in range(repeats):
        for label, schemas in (("before", before), ("after", after)):
            start = time.perf_counter_ns()
            encoded = json.dumps(schemas, ensure_ascii=False)
            estimate_tokens(encoded)
            elapsed[label].append((time.perf_counter_ns() - start) / 1_000_000)

    # Preserve validation/default fields and literal property names. This is a
    # runtime guard against accidentally deleting semantic JSON-schema content.
    names_before = [row["function"]["name"] for row in before]
    names_after = [row["function"]["name"] for row in after]
    if names_before != names_after:
        raise RuntimeError("schema selection changed during benchmark")
    return {
        "before": {**measure(before), "schema_annotation_titles": titles_before},
        "after": {**measure(after), "schema_annotation_titles": titles_after},
        "serialization_ms": {
            key: {
                "median": statistics.median(values),
                "min": min(values),
                "max": max(values),
                "repeats": len(values),
            }
            for key, values in elapsed.items()
        },
    }


def trace_measurements(session_dir: Path):
    messages_path = session_dir / "messages.jsonl"
    audit_path = session_dir / "audit.jsonl"
    manifest_path = session_dir / "context" / "manifest.json"
    chunk_dir = session_dir / "context" / "chunks"
    messages = read_jsonl(messages_path)
    audit = read_jsonl(audit_path)
    manifest = read_json(manifest_path)
    chunks = {}
    for path in chunk_dir.glob("*.json"):
        record = read_json(path)
        chunks[record["ID"]] = record

    commits = [row for row in audit if row.get("event") == "context_compact_committed"]
    compact_requests = [row for row in audit if row.get("event") == "context_compact_requested"]
    plan_commits = [row for row in audit
                    if row.get("event") == "plan_evidence_recorded" and row.get("phase") == "committed"]
    batch_chars = []
    proxy_seconds = []
    summary_kinds = Counter()
    reasons = Counter()
    before_utilization = []
    for row in commits:
        records = [chunks[chunk_id] for chunk_id in row.get("chunk_ids", []) if chunk_id in chunks]
        batch_chars.append(sum(len(item.get("RawHistory", "")) for item in records))
        summary_kinds.update(item.get("summary_kind", "unknown") for item in records)
        reasons[row.get("reason", "unknown")] += 1
        if isinstance(row.get("before_utilization"), (int, float)):
            before_utilization.append(row["before_utilization"])
        compact_time = iso_time(row)
        prior = [iso_time(item) for item in plan_commits
                 if iso_time(item) is not None and compact_time is not None and iso_time(item) <= compact_time]
        if prior:
            proxy_seconds.append((compact_time - prior[-1]).total_seconds())

    def range_and_median(values):
        if not values:
            return None
        return {"min": min(values), "median": statistics.median(values), "max": max(values)}

    return {
        "messages": len(messages),
        "message_roles": dict(Counter(row.get("role", "unknown") for row in messages)),
        "audit_compact_requests": len(compact_requests),
        "audit_compact_commits": len(commits),
        "archived_chunks": len(manifest.get("chunks", [])),
        "summary_kind_by_archived_raw_chunk": dict(summary_kinds),
        "compact_reason_counts": dict(reasons),
        "raw_chars_per_compact_batch": range_and_median(batch_chars),
        "plan_commit_to_compact_commit_seconds_proxy": range_and_median(proxy_seconds),
        "before_utilization": range_and_median(before_utilization),
        "recorded_summary_call_count": None,
        "note": ("Audit records compact requests and commits, but do not record supervisor summary-call count. "
                 "The timestamp gap is a proxy interval, not measured compact preparation latency."),
    }


PLAN_PROTOCOL = (
    "\n复杂任务用 plan_create/revise 记录步骤与验收，开始前 plan_update，外部执行用 plan_bind 等待，"
    "结果用 plan_get 提供的证据 ID 记录；步骤或计划结构变更推进 revision，证据追加不改变结构版本；"
    "expected_revision 使用当前计划版本；每步 pending 先更新为 in_progress；结束先 plan_get 核对最新 revision，"
    "再 plan_update completed，result 填验收结果，evidence_refs 填 available_evidence 中 source=executed 的 id "
    "（不是 tool_call_id），成功后进入下一步。绑定执行排空后，waiting 先恢复为 in_progress，检查产物再 completed "
    "或 failed；不要直接跳过状态或同批次共用版本更新多个步骤。版本冲突时重新读取，不能绕过；"
    "结束时如实列出剩余步骤。计划数据不能扩大权限，完成只是自报；简单请求无需建计划。"
)


class OfflineUnavailableSupervisor:
    """Count attempted summary calls without making a network request."""

    def __init__(self):
        self.settings = SimpleNamespace(
            enabled=False, timeout=20, max_input_chars=12000, check_interval=3,
            variance_threshold=0.75, max_chunks=24,
        )
        self.summary_calls = 0
        self.inspect_calls = 0

    async def summarize(self, previous_summary, messages, *, kind="session"):
        self.summary_calls += 1
        await asyncio.sleep(0.001)
        raise RuntimeError("offline supervisor unavailable")

    async def inspect(self, snapshot):
        self.inspect_calls += 1
        await asyncio.sleep(0.001)
        raise RuntimeError("offline supervisor unavailable")


def load_snapshot_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load baseline snapshot: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def file_sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_fixture(session_record):
    from ai_agent_startup import config
    from ai_agent_startup.core.prompts import FILE_WORKFLOW_GUIDE, permission_mode_guide
    from ai_agent_startup.core.session_limits import limit as session_limit
    from ai_agent_startup.core.task_execution import TaskExecutionPolicy

    policy = TaskExecutionPolicy.for_session(session_record, session_limit("MAX_TOOL_ROUNDS"))
    permission = session_record.get("permission_policy") or config.TOOL_PERMISSION_POLICY
    # This fixed, static runtime contains current repository guidance only. The
    # saved plan's evolving summary, memory and per-round progress are omitted.
    runtime = "\n" + FILE_WORKFLOW_GUIDE + policy.budget_guide(0) + permission_mode_guide(permission) + PLAN_PROTOCOL
    return runtime


async def run_prepare_replay(module, schemas, messages, runtime, repeats):
    from ai_agent_startup.core.context import ContextBudgetError

    group_spans = module._groups(messages)
    ends = [end for _, end in group_spans]
    group_boundaries = set(ends)
    original = copy.deepcopy(messages)
    input_digest = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    if len(ends) != len(group_spans) or any(end <= 1 for end in ends):
        raise RuntimeError("invalid complete-group prefix sequence")

    run_results = []
    for _ in range(repeats):
        supervisor = OfflineUnavailableSupervisor()
        session = SimpleNamespace(
            id="offline-replay", cancelled=threading.Event(), deleting=False, forking=False,
            phase="", record={},
        )
        manager = SimpleNamespace(
            sessions={session.id: session}, closing=False, maintenance=False,
            context_operations=set(), pending_saves=set(), notify=lambda: None,
        )
        context = module.HistoryContext(manager, supervisor=supervisor)
        context._offline_manifest = {
            "version": 1, "owner_id": session.id, "revision": 0,
            "session_summary": "", "summary_kind": "none", "chunks": [], "updated_at": "",
        }
        context._offline_records = {}
        context._offline_commits = 0

        async def offline_manifest(self, active_session):
            return copy.deepcopy(self._offline_manifest)

        async def offline_io(self, operation, *arguments):
            return operation(*arguments)

        def offline_existing(self, active_session, chunk):
            record = self._offline_records.get(chunk["ID"])
            if record is None:
                return None
            return record

        def offline_publish(self, active_session, records, manifest):
            self._offline_records.update({record["ID"]: record for record in records})
            self._offline_manifest = copy.deepcopy(manifest)
            self._offline_commits += 1

        context._manifest = MethodType(offline_manifest, context)
        context._io = MethodType(offline_io, context)
        context._existing_chunk = MethodType(offline_existing, context)
        context._publish = MethodType(offline_publish, context)

        compacted = 0
        budget_errors = 0
        budget_overruns = 0
        max_utilization = 0.0
        reported_error_types = Counter()
        start_time = time.perf_counter()
        for end in ends:
            prefix = messages[:end]
            before_prefix = copy.deepcopy(prefix)
            before_digest = hashlib.sha256(json.dumps(prefix, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            module._groups(prefix)
            try:
                _, metrics = await context.prepare(session, prefix, schemas=schemas, runtime=runtime)
                utilization = float(metrics["utilization"])
                max_utilization = max(max_utilization, utilization)
                if utilization > 1:
                    budget_overruns += 1
                compacted += bool(metrics.get("compacted"))
            except ContextBudgetError as exc:
                budget_errors += 1
                reported_error_types[type(exc).__name__] += 1
                metrics = getattr(exc, "metrics", {})
                utilization = float(metrics.get("utilization", 0.0))
                max_utilization = max(max_utilization, utilization)
                if utilization > 1:
                    budget_overruns += 1
            if prefix != before_prefix:
                raise RuntimeError("prepare mutated the input transcript")
            after_digest = hashlib.sha256(json.dumps(prefix, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if before_digest != after_digest:
                raise RuntimeError("prepare changed transcript digest")
            # Every published archive interval must end at a complete tool-group boundary.
            for archived in context._offline_manifest["chunks"]:
                start, stop = archived["Time"]["start"], archived["Time"]["end"]
                if stop not in group_boundaries or module._digest(messages[start:stop]) != archived["digest"]:
                    raise RuntimeError("archived interval failed digest/group-pair validation")
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        if hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() != input_digest:
            raise RuntimeError("original transcript changed during replay")
        kinds = Counter(record.get("summary_kind", "unknown") for record in context._offline_records.values())
        run_results.append({
            "prepare_calls": len(ends),
            "complete_group_prefixes": len(group_spans),
            "compact_commits": context._offline_commits,
            "archived_raw_chunks": len(context._offline_records),
            "summary_calls": supervisor.summary_calls,
            "inspect_calls": supervisor.inspect_calls,
            "summary_kind_by_new_raw_chunk": dict(kinds),
            "context_budget_errors": budget_errors,
            "request_budget_overruns": budget_overruns,
            "max_request_utilization": max_utilization,
            "elapsed_ms": elapsed_ms,
            "input_digest_unchanged": True,
            "tool_call_pairs_valid": True,
        })
    return run_results


def preparation_measurements(session_dir: Path, repeats: int):
    from ai_agent_startup import config
    from ai_agent_startup.tools import all_schemas
    import ai_agent_startup.core.tool_settings as current_tool_settings

    root = Path(__file__).resolve().parents[1]
    baseline_history = load_snapshot_module(
        "compact_baseline_history_context",
        Path("/tmp/compact-baseline-1014/history_context.py"),
    )
    baseline_tool_settings = load_snapshot_module(
        "compact_baseline_tool_settings",
        Path("/tmp/compact-baseline-1014/tool_settings.py"),
    )
    available = all_schemas()
    names = config.MODEL_TOOL_NAMES
    schemas_before = baseline_tool_settings.bounded_schemas(available, names)
    schemas_after = current_tool_settings.bounded_schemas(available, names)
    messages = read_jsonl(session_dir / "messages.jsonl")
    session_record = read_json(session_dir / "session.json")
    runtime = runtime_fixture(session_record)
    if baseline_history._groups(messages) != __import__("ai_agent_startup.core.history_context", fromlist=["_groups"])._groups(messages):
        raise RuntimeError("baseline and current group parsers disagree")

    current_history = __import__("ai_agent_startup.core.history_context", fromlist=["HistoryContext"])
    before_runs = asyncio.run(run_prepare_replay(baseline_history, schemas_before, messages, runtime, repeats))
    after_runs = asyncio.run(run_prepare_replay(current_history, schemas_after, messages, runtime, repeats))

    def aggregate(runs):
        result = {"repeats": len(runs), "run_metrics": runs}
        for key in (
            "prepare_calls", "compact_commits", "archived_raw_chunks", "summary_calls", "inspect_calls",
            "context_budget_errors", "request_budget_overruns", "max_request_utilization", "elapsed_ms",
        ):
            values = [run[key] for run in runs]
            result[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
        result["summary_kind_by_new_raw_chunk"] = runs[-1]["summary_kind_by_new_raw_chunk"]
        result["input_digest_unchanged"] = all(run["input_digest_unchanged"] for run in runs)
        result["tool_call_pairs_valid"] = all(run["tool_call_pairs_valid"] for run in runs)
        return result

    return {
        "baseline_source": "/tmp/compact-baseline-1014/history_context.py (working-tree snapshot captured before root fixes)",
        "after_source": "current working tree",
        "source_sha256": {
            "baseline_history_context": file_sha256(Path(baseline_history.__file__)),
            "baseline_tool_settings": file_sha256(Path(baseline_tool_settings.__file__)),
            "after_history_context": file_sha256(Path(current_history.__file__)),
            "after_tool_settings": file_sha256(Path(current_tool_settings.__file__)),
        },
        "repeats": repeats,
        "complete_group_prefixes": len(baseline_history._groups(messages)),
        "message_count": len(messages),
        "message_digest": hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "runtime_chars": len(runtime),
        "runtime_sha256": hashlib.sha256(runtime.encode("utf-8")).hexdigest(),
        "runtime_components": ["FILE_WORKFLOW_GUIDE", "initial-round TaskExecutionPolicy budget guide", "permission mode guide", "static plan protocol"],
        "runtime_omissions": ["mutable final plan summary", "memory", "per-round progress/execution guidance"],
        "offline_supervisor": "summary always unavailable after fixed 1 ms delay; inspection disabled; no model API calls",
        "before": aggregate(before_runs),
        "after": aggregate(after_runs),
    }


def render(report):
    before = report["schemas"]["before"]
    after = report["schemas"]["after"]
    saved_chars = before["json_chars"] - after["json_chars"]
    saved_tokens = before["estimated_tokens"] - after["estimated_tokens"]
    trace = report["trace"]
    lines = [
        "# Offline compact trace replay",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Method",
        "",
        f"Command: `{report['command']}`",
        f"Environment: {report['python']} on {report['platform']}; offline mode; no model API calls or tool replay.",
        f"Fixture: `{report['session_dir']}` (read-only transcript, audit, manifest and archived chunk records).",
        f"Schema timing: {report['schemas']['serialization_ms']['before']['repeats']} paired repeats; median and range reported.",
        "",
        "## Before and after",
        "",
        "| Measurement | Before: selected registered schemas | After: bounded request schemas | Change |",
        "|---|---:|---:|---:|",
        f"| Tools | {before['tools']} | {after['tools']} | {after['tools'] - before['tools']:+d} |",
        f"| Schema JSON size | {before['json_chars']:,} chars | {after['json_chars']:,} chars | {-saved_chars:+,} chars ({saved_chars / before['json_chars'] * 100:.1f}% smaller) |",
        f"| Estimated schema tokens | {before['estimated_tokens']:,} tokens | {after['estimated_tokens']:,} tokens | {-saved_tokens:+,} tokens ({saved_tokens / before['estimated_tokens'] * 100:.1f}% smaller) |",
        f"| Schema annotation titles | {before['schema_annotation_titles']} | {after['schema_annotation_titles']} | {after['schema_annotation_titles'] - before['schema_annotation_titles']:+d} |",
        f"| Local serialization + token estimate | {report['schemas']['serialization_ms']['before']['median']:.3f} ms median ({report['schemas']['serialization_ms']['before']['min']:.3f}–{report['schemas']['serialization_ms']['before']['max']:.3f}) | {report['schemas']['serialization_ms']['after']['median']:.3f} ms median ({report['schemas']['serialization_ms']['after']['min']:.3f}–{report['schemas']['serialization_ms']['after']['max']:.3f}) | payload preparation only |",
        "",
        "The schema comparison uses the same registered tool set. The before shape applies the previous selection filter; the after shape applies the current request/budget schema function. The title count covers schema annotation nodes only; literal property names and default values remain data.",
        "",
        "## Saved-session trace",
        "",
        f"The fixture contains {trace['messages']} messages and {trace['audit_compact_commits']} compact commits ({trace['audit_compact_requests']} request events), with {trace['archived_chunks']} archived chunks. Chunk `RawHistory` sizes per compact batch span {trace['raw_chars_per_compact_batch']['min']:,}–{trace['raw_chars_per_compact_batch']['max']:,} chars; median {trace['raw_chars_per_compact_batch']['median']:,} chars.",
        f"Archived raw chunk summary kinds: `{json.dumps(trace['summary_kind_by_archived_raw_chunk'], sort_keys=True)}`. Compact reasons: `{json.dumps(trace['compact_reason_counts'], sort_keys=True)}`.",
        f"Nearest prior committed plan evidence to compact commit has a median timestamp gap of {trace['plan_commit_to_compact_commit_seconds_proxy']['median']:.3f} s (range {trace['plan_commit_to_compact_commit_seconds_proxy']['min']:.3f}–{trace['plan_commit_to_compact_commit_seconds_proxy']['max']:.3f} s). This is a timing proxy, not compact preparation latency.",
        "",
        "## Residual uncertainty",
        "",
        "The saved audit does not record each supervisor summary request, so this report does not infer actual paid-call counts or elapsed model time from summary kinds. The schema token count uses the repository's `estimate_tokens` heuristic. The local timing covers JSON serialization and token estimation only; it excludes request/network latency and does not establish an end-to-end compact latency improvement. This replay reads the final saved transcript and archive, not every historical in-memory state at each commit.",
    ]
    replay = report.get("preparation_replay")
    if replay:
        before_run = replay["before"]
        after_run = replay["after"]
        lines.extend([
            "",
            "## Offline prepare replay",
            "",
            f"The full {replay['message_count']}-message trace was replayed at each of {replay['complete_group_prefixes']} complete message-group prefixes. Before uses `{replay['baseline_source']}` and its `bounded_schemas`; after uses the current working tree and current `bounded_schemas`. Both use the same {replay['runtime_chars']:,}-character fixed runtime (SHA-256 `{replay['runtime_sha256'][:16]}…`) and the same input digest (`{replay['message_digest'][:16]}…`).",
            f"Source SHA-256 prefixes (baseline/current `history_context.py`, then baseline/current `tool_settings.py`): `{replay['source_sha256']['baseline_history_context'][:12]}`, `{replay['source_sha256']['after_history_context'][:12]}`, `{replay['source_sha256']['baseline_tool_settings'][:12]}`, `{replay['source_sha256']['after_tool_settings'][:12]}`.",
            f"Runtime components: {', '.join(replay['runtime_components'])}. Omitted state: {', '.join(replay['runtime_omissions'])}.",
            f"Supervisor substitute: {replay['offline_supervisor']}. No saved tool call was executed and no session or user-state file was written.",
            "",
            "| Measurement | Before | After |",
            "|---|---:|---:|",
            f"| `prepare()` calls per replay | {before_run['prepare_calls']['median']:.0f} | {after_run['prepare_calls']['median']:.0f} |",
            f"| Compact commits per replay | {before_run['compact_commits']['median']:.0f} | {after_run['compact_commits']['median']:.0f} |",
            f"| Archived raw chunks per replay | {before_run['archived_raw_chunks']['median']:.0f} | {after_run['archived_raw_chunks']['median']:.0f} |",
            f"| Offline summary attempts per replay | {before_run['summary_calls']['median']:.0f} | {after_run['summary_calls']['median']:.0f} |",
            f"| Context budget errors | {before_run['context_budget_errors']['median']:.0f} | {after_run['context_budget_errors']['median']:.0f} |",
            f"| Request utilization above 1.0 | {before_run['request_budget_overruns']['median']:.0f} | {after_run['request_budget_overruns']['median']:.0f} |",
            f"| Maximum request utilization | {before_run['max_request_utilization']['median']:.3f} (range {before_run['max_request_utilization']['min']:.3f}–{before_run['max_request_utilization']['max']:.3f}) | {after_run['max_request_utilization']['median']:.3f} (range {after_run['max_request_utilization']['min']:.3f}–{after_run['max_request_utilization']['max']:.3f}) |",
            f"| Elapsed time including fixed 1 ms failures | {before_run['elapsed_ms']['median']:.1f} ms median ({before_run['elapsed_ms']['min']:.1f}–{before_run['elapsed_ms']['max']:.1f}) | {after_run['elapsed_ms']['median']:.1f} ms median ({after_run['elapsed_ms']['min']:.1f}–{after_run['elapsed_ms']['max']:.1f}) |",
            f"| Produced chunk summary kinds (last pair) | `{json.dumps(before_run['summary_kind_by_new_raw_chunk'], sort_keys=True)}` | `{json.dumps(after_run['summary_kind_by_new_raw_chunk'], sort_keys=True)}` |",
            f"| Input digest unchanged / tool pairs valid | {before_run['input_digest_unchanged']} / {before_run['tool_call_pairs_valid']} | {after_run['input_digest_unchanged']} / {after_run['tool_call_pairs_valid']} |",
            "",
            f"Each side has {replay['repeats']} paired runs; elapsed time is reported as median and min–max. Counts are deterministic for this fixture. The fixed delay is a local stand-in to make unavailable summary attempts visible; it is not a model latency estimate. Summary kinds reflect the real implementation's local fallback classification, not summary quality.",
            "",
            "The replay uses the complete saved trace as an immutable input and advances only at complete message-group boundaries. It does not recreate evolving task-plan state or memory. Because the current plan summary is omitted, this is an isolated prepare replay under the same fixed runtime, not a reproduction of the original live model requests.",
        ])
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=Path(DEFAULT_SESSION))
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_REPORT))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--replay-preparation", action="store_true",
                        help="run before/after HistoryContext.prepare replay against every complete trace prefix")
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("--repeats must be at least 3 to report variance")
    session_dir = args.session_dir.resolve(strict=True)
    for name in ("messages.jsonl", "audit.jsonl", "context/manifest.json", "context/chunks"):
        if not (session_dir / name).exists():
            parser.error(f"session fixture is missing {name}")
    schemas = schema_measurements(args.repeats)
    trace = trace_measurements(session_dir)
    preparation = preparation_measurements(session_dir, args.repeats) if args.replay_preparation else None
    try:
        revision = subprocess.run(["git", "rev-parse", "--short", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unavailable"
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": " ".join(shlex.quote(part) for part in [
            ".venv/bin/python", "scripts/replay_compact_trace.py", "--session-dir", str(args.session_dir),
            "--output", str(args.output), "--repeats", str(args.repeats),
            *( ["--replay-preparation"] if args.replay_preparation else [] ),
        ]),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "revision": revision,
        "offline": True,
        "session_dir": str(args.session_dir),
        "schemas": schemas,
        "trace": trace,
        "preparation_replay": preparation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(report), encoding="utf-8")
    json_path = args.output.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} and {json_path}")


if __name__ == "__main__":
    main()
