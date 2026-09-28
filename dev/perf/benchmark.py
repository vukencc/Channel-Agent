"""隔离性能基准；生成数据只用于测量开销，不用于检索质量评估。"""
import argparse
import json
import resource
import tempfile
import time
import statistics
from pathlib import Path


def sessions(count, megabytes):
    from core.storage import SessionStore
    from core.sessions import SessionManager
    with tempfile.TemporaryDirectory(prefix='agent-perf-') as directory:
        store = SessionStore(Path(directory) / 'state', Path(directory) / 'workspace')
        try:
            for index in range(count):
                record = store.new_record(str(index), '基准系统提示')
                record['messages'] += [{'role': 'user', 'content': 'x' * 65536}
                                       for _ in range(megabytes * 16)]
                store.save(record)
            del record
            before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            start = time.perf_counter()
            manager = SessionManager(store)
            elapsed = time.perf_counter() - start
            return {'seconds': elapsed, 'peak_rss_delta_mib':
                    (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024,
                    'sessions': len(manager.sessions), 'mib_per_session': megabytes}
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case', choices=['sessions', 'context', 'quota', 'audit', 'export'])
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--megabytes', type=int, default=10)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'case': args.case, **(sessions(args.count, args.megabytes)
                                   if args.case == 'sessions' else
                                   context() if args.case == 'context' else
                                   quota() if args.case == 'quota' else
                                   audit() if args.case == 'audit' else export())}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result))


def context():
    from core.context import build_model_history
    messages = [{'role': 'system', 'content': '基准'}]
    for i in range(5000):
        messages += [{'role': 'user', 'content': f'问题 {i}'},
                     {'role': 'assistant', 'content': '中文与 ASCII abc 混合段落。' * 100}]
    elapsed = []
    for _ in range(3):
        start = time.perf_counter()
        _, metrics = build_model_history(messages)
        elapsed.append(time.perf_counter() - start)
    return {'messages': len(messages), 'seconds': elapsed,
            'median_seconds': statistics.median(elapsed), 'metrics': metrics}


def quota():
    import threading
    from tools import command
    from tools.sandbox import ToolContext, tool_context
    with tempfile.TemporaryDirectory(prefix='agent-quota-') as directory:
        root = Path(directory) / 'workspace'
        root.mkdir()
        for i in range(3000):
            (root / str(i)).write_text('x')
        original = command.check_workspace_quota
        calls = 0
        scan_seconds = 0
        def checked():
            nonlocal calls, scan_seconds
            calls += 1
            start = time.perf_counter()
            try:
                return original()
            finally:
                scan_seconds += time.perf_counter() - start
        command.check_workspace_quota = checked
        try:
            with tool_context(ToolContext(root, Path(directory) / 'audit.jsonl',
                                          lambda *_: True, threading.Event())):
                start, cpu = time.perf_counter(), time.process_time()
                result = command.run_command('python3 -c "import os,time; '
                    "[(os.write(1,b'x'*8192),time.sleep(.005)) for _ in range(100)]\"")
                if not result.startswith('[退出码] 0'):
                    raise RuntimeError('真实沙箱基准失败：' + result[:1000])
                return {'seconds': time.perf_counter() - start,
                        'cpu_seconds': time.process_time() - cpu,
                        'scan_seconds': scan_seconds, 'scans': calls, 'files': 3000}
        finally:
            command.check_workspace_quota = original


def audit():
    import threading
    from tools.sandbox import ToolContext, tool_context, audit as write_audit
    with tempfile.TemporaryDirectory(prefix='agent-audit-') as directory:
        root = Path(directory)
        with tool_context(ToolContext(root, root / 'audit.jsonl', lambda *_: True, threading.Event())):
            start = time.perf_counter()
            for i in range(10000):
                write_audit('benchmark', number=i)
            elapsed = time.perf_counter() - start
        lines = (root / 'audit.jsonl').read_text().splitlines()
        assert len(lines) == 10000
        return {'seconds': elapsed, 'events': len(lines)}


def export():
    import tracemalloc
    from core.storage import SessionStore
    with tempfile.TemporaryDirectory(prefix='agent-export-') as directory:
        store = SessionStore(Path(directory) / 'state', Path(directory) / 'workspace')
        try:
            record = store.new_record('export', 'system')
            record['messages'] += [{'role': 'user', 'content': 'x' * 65536} for _ in range(1600)]
            store.save(record)
            identifier = record['id']
            del record
            tracemalloc.start()
            start = time.perf_counter()
            path = store.export_saved(identifier, 'json')
            elapsed = time.perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return {'seconds': elapsed, 'python_peak_mib': peak / 1024 ** 2,
                    'output_bytes': path.stat().st_size, 'source_mib': 100}
        finally:
            store.close()


if __name__ == '__main__':
    main()
