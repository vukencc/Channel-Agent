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
    parser.add_argument('case', choices=['sessions', 'context', 'quota', 'audit', 'export', 'rag', 'vector', 'bm25'])
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--megabytes', type=int, default=10)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-ref', help='审计复测可直接使用指定 Git 提交中的原始实现')
    parser.add_argument('--corpus', type=Path, help='RAG 基准使用已有公开 parquet 与固定 quality.json')
    args = parser.parse_args()
    result = {'case': args.case, **(sessions(args.count, args.megabytes)
                                   if args.case == 'sessions' else
                                   context() if args.case == 'context' else
                                   quota() if args.case == 'quota' else
                                   audit(args.baseline_ref) if args.case == 'audit' else
                                   rag(args.corpus, args.output) if args.case == 'rag' else
                                   vector(args.output) if args.case == 'vector' else
                                   bm25(args.output) if args.case == 'bm25' else export())}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'traces'}))


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


def audit(baseline_ref=None):
    import threading
    from tools import sandbox
    write_audit = sandbox.audit
    if baseline_ref:
        import ast
        import re
        import subprocess
        if not re.fullmatch(r'[0-9a-f]{7,40}', baseline_ref):
            raise ValueError('基线必须是明确的 Git commit hash')
        source = subprocess.check_output(['git', 'show', baseline_ref + ':tools/sandbox.py'], text=True)
        tree = ast.parse(source)
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name in {'audit', '_audit_path'}]
        namespace = dict(vars(sandbox))
        exec(compile(ast.Module(body=definitions, type_ignores=[]), '<historical-audit>', 'exec'), namespace)
        write_audit = namespace['audit']
    samples = []
    for _ in range(5):
        with tempfile.TemporaryDirectory(prefix='agent-audit-') as directory:
            root = Path(directory)
            with sandbox.tool_context(sandbox.ToolContext(root, root / 'audit.jsonl', lambda *_: True, threading.Event())):
                start = time.perf_counter()
                for i in range(10000):
                    write_audit('benchmark', number=i)
                samples.append(time.perf_counter() - start)
            lines = (root / 'audit.jsonl').read_text().splitlines()
            assert [json.loads(line)['number'] for line in lines] == list(range(10000))
    return {'seconds': samples, 'median_seconds': statistics.median(samples),
            'events_per_sample': 10000, 'baseline_ref': baseline_ref}


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


def rag(corpus, output):
    import hashlib
    import pyarrow.parquet as pq
    import config
    from rag.index import RetrievalIndex
    from rag.tool import rag_search
    from dev.rag.quality import metrics
    manifest_path = Path('dev/rag/inputs/quality.json')
    manifest = json.loads(manifest_path.read_text())
    with corpus.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['corpus_sha256']:
            raise ValueError('公开语料哈希不匹配')
    wanted = set(manifest['document_ids'])
    documents = []
    for batch in pq.ParquetFile(corpus).iter_batches(columns=['id', 'text']):
        for row in batch.to_pylist():
            if str(row['id']) in wanted:
                documents.append({'content': row['text'], 'metadata': {'doc_id': str(row['id']),
                                  'filename': str(row['id']) + '.txt', 'source': 'C-MTEB/T2Retrieval'}})
    if {doc['metadata']['doc_id'] for doc in documents} != wanted:
        raise ValueError('固定候选池缺失文档')
    documents.sort(key=lambda row: row['metadata']['doc_id'])
    config.RAG_CACHE_DIR = output.parent / (output.stem + '-cache')
    start = time.perf_counter()
    index = RetrievalIndex(documents)
    build = time.perf_counter() - start
    traces = []
    for repetition in range(2):
        for query in manifest['queries']:
            trace = {}
            rag_search(query['text'], top_k=10, index=index, trace=trace)
            traces.append({'id': query['id'], 'repetition': repetition, **trace})
            print(f"RAG {repetition + 1}/2 {query['id']}", flush=True)
    summaries = []
    for repetition in range(2):
        rows = traces[repetition * len(manifest['queries']):(repetition + 1) * len(manifest['queries'])]
        summaries.append({'p50_seconds': statistics.median(row['timings_seconds']['total'] for row in rows),
            'p50_rerank_seconds': statistics.median(row['timings_seconds']['rerank'] for row in rows),
            'quality': {stage: metrics([{**query, 'ranking': [(row['metadata']['doc_id'], row['score'])
                        for row in trace['stages'][stage]]} for query, trace in zip(manifest['queries'], rows)
                        if query['split'] == 'evaluation'], config.RAG_THRESHOLD_NORMAL if stage == 'rerank' else float('-inf'))
                        for stage in ('vector', 'bm25', 'rrf', 'rerank')}})
    return {'documents': len(documents), 'queries': len(manifest['queries']), 'build_seconds': build,
            'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            'rounds': summaries, 'traces': traces}


def vector(output):
    """随机单位向量用于索引内核开销/近似召回，不代表语义检索质量。"""
    import numpy as np
    import config
    from rag.index import RetrievalIndex
    from rag.result_cache import ResultCache
    rng = np.random.default_rng(20260928)
    vectors = rng.normal(size=(100000, 384)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    queries = rng.normal(size=(20, 384)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    class QueryInput:
        def embed_queries(self, texts):
            return np.asarray([queries[int(text)] for text in texts])
    index = RetrievalIndex.__new__(RetrievalIndex)
    index.children = range(len(vectors))
    index.parents = [{'id': str(i)} for i in range(len(vectors))]
    index.parent_indexes = np.arange(len(vectors), dtype=np.int32)
    index.vectors, index.model, index.query_cache = vectors, QueryInput(), ResultCache()
    config.RAG_CACHE_DIR = output.parent / (output.stem + '-cache')
    elapsed, recall = [], []
    for i, query in enumerate(queries):
        reference = set(np.argsort(-(vectors @ query), kind='stable')[:50].tolist())
        start = time.perf_counter()
        rows = index.dense(str(i), 50)
        elapsed.append(time.perf_counter() - start)
        recall.append(len(reference & {int(row['id']) for row in rows}) / 50)
    if config.RAG_VECTOR_BACKEND == 'ann' and index.vector_backend != 'ann':
        raise RuntimeError('ANN 基准实际回退到 exact，不能记作 ANN 通过')
    return {'children': len(vectors), 'dimensions': 384, 'queries': len(queries),
            'seed': 20260928, 'backend': getattr(index, 'vector_backend', 'exact'),
            'requested_backend': getattr(config, 'RAG_VECTOR_BACKEND', 'exact'),
            'first_seconds': elapsed[0], 'p50_seconds': statistics.median(elapsed[1:]),
            'p95_seconds': float(np.percentile(elapsed[1:], 95)), 'recall_at_50': statistics.mean(recall)}


def bm25(output):
    import config
    from rag.lexical import BM25Index, tokenize
    texts = [f'文档 {i} 苹果 服务 检索 独立记录 {i % 97}' for i in range(20000)]
    identifiers = [str(i) for i in range(len(texts))]
    def build():
        if getattr(config, 'RAG_BM25_PERSIST', False):
            from rag.lexical_store import PersistentBM25
            return PersistentBM25(output.with_suffix('.sqlite'), texts, identifiers)
        return BM25Index(texts)
    tokenize.cache_clear()
    start = time.perf_counter()
    index = build()
    initial = time.perf_counter() - start
    texts[100] += ' 唯一增量内容'
    start = time.perf_counter()
    index = index.with_updates({'100': texts[100]}, []) if hasattr(index, 'with_updates') else build()
    changed = time.perf_counter() - start
    after = index.search('唯一增量内容', 10)
    tokenize.cache_clear()
    start = time.perf_counter()
    restored = build()
    restart = time.perf_counter() - start
    assert restored.search('唯一增量内容', 10) == after
    return {'documents': len(texts), 'initial_seconds': initial,
            'one_update_seconds': changed, 'warm_restart_seconds': restart,
            'persistent': getattr(config, 'RAG_BM25_PERSIST', False), 'matches': after}


if __name__ == '__main__':
    main()
