"""真实本地 RAG 的单请求/四会话竞争；文件探针使用独立临时工作区。"""
import argparse
import asyncio
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path


async def measure(corpus: Path) -> dict:
    import pyarrow.parquet as pq
    import config
    from core.sessions import SessionManager
    from core.storage import SessionStore
    from rag.index import get_index
    from rag.rerank import get_reranker

    manifest = json.loads(Path('dev/rag/inputs/quality.json').read_text())
    with corpus.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['corpus_sha256']:
            raise ValueError('公开语料哈希不匹配')
    wanted = set(manifest['document_ids'])
    with tempfile.TemporaryDirectory(prefix='agent-concurrency-') as directory:
        root = Path(directory)
        config.DOC_DIR = root / 'docs'
        config.DOC_DIR.mkdir()
        config.RAG_CACHE_DIR = root / 'cache'
        config.RAG_QUERY_CACHE_SIZE = config.RAG_RERANK_CACHE_SIZE = 0
        for batch in pq.ParquetFile(corpus).iter_batches(columns=['id', 'text']):
            for row in batch.to_pylist():
                if str(row['id']) in wanted:
                    (config.DOC_DIR / (str(row['id']) + '.txt')).write_text(row['text'])
        # 预加载真实模型/索引；计时不含下载/冷启动，不启用结果缓存。
        get_index()
        get_reranker()
        store = SessionStore(root / 'state', root / 'workspaces')
        manager = SessionManager(store)
        sessions = [manager.create(str(index)) for index in range(4)]
        probe = manager.create('文件探针')
        (store.workspace(probe.id) / 'probe.txt').write_text('文件探针内容')
        queries = [row['text'] for row in manifest['queries'][:4]]
        async def search(index):
            started = time.perf_counter()
            result = await manager._tool(sessions[index], {'id': f'query-{index}', 'function': {
                'name': 'rag_search', 'arguments': json.dumps({'query': queries[index]})}})
            if '[超时]' in result or '[错误]' in result or '[已取消]' in result:
                raise RuntimeError(result)
            return time.perf_counter() - started, hashlib.sha256(result.replace(str(root), '<tmp>').encode()).hexdigest()
        try:
            singles = [await search(index) for index in range(4)]
            start = time.perf_counter()
            tasks = [asyncio.create_task(search(index)) for index in range(4)]
            await asyncio.sleep(.05)
            probe_start = time.perf_counter()
            result = await manager._tool(probe, {'id': 'probe', 'function': {
                'name': 'read_file', 'arguments': '{"path":"probe.txt"}'}})
            probe_seconds = time.perf_counter() - probe_start
            assert '文件探针内容' in result
            concurrent = await asyncio.gather(*tasks)
            wall = time.perf_counter() - start
            assert [row[1] for row in singles] == [row[1] for row in concurrent]
            return {'rag_concurrency': getattr(config, 'RAG_INFERENCE_CONCURRENCY', 0),
                    'single_seconds': [row[0] for row in singles],
                    'single_p95_seconds': max(row[0] for row in singles),
                    'single_median_seconds': statistics.median(row[0] for row in singles),
                    'four_session_seconds': [row[0] for row in concurrent],
                    'four_session_wall_seconds': wall, 'file_probe_seconds': probe_seconds,
                    'result_hashes': [row[1] for row in singles],
                    'method': '固定公开清单前四查询，缓存关闭；p95 为四样本 nearest-rank 最大值'}
        finally:
            await manager.shutdown()
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = asyncio.run(measure(args.corpus))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(data, ensure_ascii=False))
