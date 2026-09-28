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
    parser.add_argument('case', choices=['sessions', 'context'])
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--megabytes', type=int, default=10)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'case': args.case, **(sessions(args.count, args.megabytes)
                                   if args.case == 'sessions' else context())}
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


if __name__ == '__main__':
    main()
