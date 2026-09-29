"""Compare synchronous historical CLI rendering with the working tree version.

The historical AgentCLI class is loaded from ``git show HEAD:...`` in memory;
the working tree's production files are never replaced. Synthetic messages and
all session state live in a temporary directory. No model calls are made.
"""
import argparse
import asyncio
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ai_agent_startup.core.cli import AgentCLI as WorkingAgentCLI
from ai_agent_startup.core.storage import SessionStore


REPO = Path(__file__).resolve().parents[2]
BASELINE_PATH = "src/ai_agent_startup/core/cli.py"
RUNS = 9
MESSAGE_COUNT = 50_000


def historical_cli_class(baseline_ref):
    resolved = subprocess.check_output(
        ["git", "rev-parse", f"{baseline_ref}^{{commit}}"], cwd=REPO, text=True
    ).strip()
    source = subprocess.check_output(
        ["git", "show", f"{resolved}:{BASELINE_PATH}"], cwd=REPO, text=True
    )
    namespace = {"__name__": "ai_agent_startup.core.cli_historical"}
    exec(compile(source, f"{resolved}:{BASELINE_PATH}", "exec"), namespace)
    return namespace["AgentCLI"], resolved, hashlib.sha256(source.encode()).hexdigest()


def fixture_messages():
    messages = [{"role": "system", "content": "Synthetic CLI performance fixture."}]
    messages.extend(
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": f"message-{index:05d} sample text"}
        for index in range(MESSAGE_COUNT)
    )
    return messages


def summary(values):
    millis = [value * 1000 for value in values]
    return {
        "median_ms": round(statistics.median(millis), 3),
        "min_ms": round(min(millis), 3),
        "max_ms": round(max(millis), 3),
        "samples_ms": [round(value, 3) for value in millis],
    }


def make_cli(cli_class, root):
    store = SessionStore(root / "state", root / "workspace")
    pipe_context = create_pipe_input()
    pipe = pipe_context.__enter__()
    cli = cli_class(store, input=pipe, output=DummyOutput())
    first = cli.active
    second = cli.manager.create("second")
    messages = fixture_messages()
    first.record["messages"] = list(messages)
    second.record["messages"] = list(messages)
    return store, pipe_context, cli, first.id, second.id, len(messages), sum(
        len(message["content"]) for message in messages
    )


async def finish_render(cli):
    drain = getattr(cli, "drain_render", None)
    if drain is not None:
        await drain()
    else:
        # Historical render is synchronous. Yield once so the heartbeat can
        # observe the event loop after the blocking work has completed.
        await asyncio.sleep(0)


async def measure_cli(prepared):
    store, pipe_context, cli, first_id, second_id, message_count, content_chars = prepared
    try:
        # Warm-up, excluded from reported samples.
        cli.transcript.__init__()
        cli._view_key = None
        cli.render(force=True)
        await finish_render(cli)

        initial = []
        for _ in range(RUNS):
            cli.transcript.__init__()
            cli._view_key = None
            started = time.perf_counter()
            cli.render(force=True)
            await finish_render(cli)
            initial.append(time.perf_counter() - started)

        switches = []
        cli.current = first_id
        for index in range(RUNS):
            target = second_id if index % 2 == 0 else first_id
            started = time.perf_counter()
            cli.select(target)
            await finish_render(cli)
            switches.append(time.perf_counter() - started)

        # Ensure the first timed repeated switch changes the active session.
        cli.select(first_id)
        await finish_render(cli)

        # Exercise repeated user-visible switches while an independent 5 ms
        # heartbeat measures the longest time the event loop cannot run.
        gaps = []
        stop = asyncio.Event()

        async def heartbeat():
            previous = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - previous)
                previous = now

        heart = asyncio.create_task(heartbeat())
        repeated_switches = []
        try:
            for index in range(RUNS):
                target = second_id if index % 2 == 0 else first_id
                started = time.perf_counter()
                cli.select(target)
                await finish_render(cli)
                repeated_switches.append(time.perf_counter() - started)
        finally:
            stop.set()
            await heart

        return {
            "fixture": {
                "messages_in_active_session": message_count,
                "message_content_chars": content_chars,
                "visible_page_lines": cli.transcript.PAGE_LINES,
                "model_calls": 0,
            },
            "first_full_render_completion": summary(initial),
            "session_switch_completion": summary(switches),
            "repeated_switch_completion": summary(repeated_switches),
            "heartbeat_interval_5ms_max_gap": {
                "max_ms": round(max(gaps) * 1000, 3) if gaps else None,
                "median_ms": round(statistics.median(gaps) * 1000, 3) if gaps else None,
                "ticks": len(gaps),
            },
        }
    finally:
        await cli.manager.shutdown()
        if getattr(cli, "_refresh_handle", None) is not None:
            cli._refresh_handle.cancel()
        if hasattr(cli, "drain_render"):
            await cli.drain_render()
        pipe_context.__exit__(None, None, None)
        store.close()


async def main(args, prepared, baseline_key, baseline_ref, baseline_commit, baseline_hash):
    results = {}
    # Allow a reversed pass to expose order effects between implementations.
    classes = [baseline_key, "working_tree"]
    if args.reverse_order:
        classes.reverse()
    for name in classes:
        results[name] = await measure_cli(prepared[name])
    result = {
        "method": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "runs_per_metric": RUNS,
            "warmup_runs": 1,
            "reverse_order": args.reverse_order,
            "baseline_ref": baseline_ref,
            "baseline_resolved_commit": baseline_commit,
            "baseline_source": f"git show {baseline_commit}:{BASELINE_PATH}",
            "baseline_source_sha256": baseline_hash,
            "completion_timing": "render call through completion of drain_render (or synchronous return plus event-loop yield for HEAD)",
            "heartbeat": "async task sleeps 5 ms during nine real select() switches; report max and median observed interval",
            "execution": "DummyOutput + PipeInput; temporary state/workspace; no model calls",
        },
        "results": results,
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


def cli_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/cli-benchmark.json"))
    parser.add_argument("--baseline-ref", default="v0.2.1",
                        help="Git ref containing the pre-optimization CLI (default: v0.2.1)")
    parser.add_argument("--reverse-order", action="store_true")
    args = parser.parse_args()
    baseline_class, baseline_commit, baseline_hash = historical_cli_class(args.baseline_ref)
    prepared = {}
    tempdirs = []
    try:
        baseline_key = f"baseline_{args.baseline_ref}"
        for name, cli_class in ((baseline_key, baseline_class),
                                ("working_tree", WorkingAgentCLI)):
            directory = tempfile.TemporaryDirectory(prefix=f"cli-bench-{name}-")
            tempdirs.append(directory)
            prepared[name] = make_cli(cli_class, Path(directory.name))
        asyncio.run(main(args, prepared, baseline_key, args.baseline_ref, baseline_commit, baseline_hash))
    finally:
        for directory in tempdirs:
            directory.cleanup()


if __name__ == "__main__":
    cli_main()
