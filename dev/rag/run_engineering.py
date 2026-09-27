"""Small, label-independent real-model engineering run; no threshold calibration."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from rag.embedding import get_embedding_model, local_model_path
from rag.index import RetrievalIndex
from rag.rerank import local_reranker_path
from rag.tool import rag_search
from dev.rag.dataset import DEFAULT_CORPUS, DEFAULT_MANIFEST, load_inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    parser.add_argument('--output', type=Path, default=ROOT / '.cache/reports/rag/engineering')
    args = parser.parse_args()
    output = args.output.resolve()
    # Inputs are immutable: results must not overwrite the manifest or corpus.
    for source in (args.manifest.resolve(), args.corpus.resolve()):
        if source.is_relative_to(output):
            parser.error('--output must not contain input files')
    documents, queries = load_inputs(args.manifest, args.corpus)
    output.mkdir(parents=True, exist_ok=True)
    # Isolate this small run from the interrupted full-corpus index.
    config.EMBEDDING_LOCAL_PATH = local_model_path()
    config.RERANK_LOCAL_PATH = local_reranker_path()
    config.RAG_CACHE_DIR = ROOT / '.cache/rag/engineering'
    manifest = {
        'purpose': 'Engineering validation only; no quality metrics or threshold calibration.',
        'selection': 'First 300 corpus IDs sorted by SHA256; first 5 previously frozen evaluation queries. No qrels used.',
        'document_ids': [doc['metadata']['doc_id'] for doc in documents],
        'queries': queries, 'embedding_identity': get_embedding_model().identity,
        'reranker': config.RERANK_MODEL,
        'parameters': {'candidates': config.RAG_CANDIDATES,
                       'rerank_top_n': config.RAG_RERANK_TOP_N, 'rrf_k': config.RAG_RRF_K},
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    start = time.perf_counter()
    index = RetrievalIndex(documents)
    build_seconds = time.perf_counter() - start
    records = []
    with (output / 'traces.jsonl').open('w') as stream:
        for query in queries:
            trace = {}
            result = rag_search(query['text'], top_k=10, index=index, trace=trace)
            stages = trace['stages']
            assert all(stages[name] for name in ('vector', 'rrf', 'rerank'))
            union = {row['id'] for name in ('vector', 'bm25') for row in stages[name]}
            assert {row['id'] for row in stages['rrf']} == union
            assert {row['id'] for row in stages['rerank']} == {
                row['id'] for row in stages['rrf'][:config.RAG_RERANK_TOP_N]}
            for rows in stages.values():
                assert [row['score'] for row in rows] == sorted(
                    (row['score'] for row in rows), reverse=True)
            trace.update(qid=query['id'], tool_output=result)
            stream.write(json.dumps(trace, ensure_ascii=False) + '\n')
            stream.flush()
            records.append(trace)
            print('query', query['id'], 'passed', round(trace['timings_seconds']['total'], 3), flush=True)
    summary = {'documents': len(documents), 'parents': len(index.parents),
               'children': len(index.children), 'queries_passed': len(records),
               'index_build_seconds': build_seconds,
               'query_seconds': [row['timings_seconds']['total'] for row in records],
               'threshold_evaluation': 'not performed', 'quality_evaluation': 'not performed'}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    lines = ['# 工程测试：真实四阶段结果', '',
             '公开语料固定哈希抽样 300 篇、固定查询 5 条；不使用 qrels，不校准或评价阈值。',
             '下表为父段落排名；分数尺度不同，不能跨阶段直接比较。完整候选及正文见 traces.jsonl。', '']
    for record in records:
        lines += [f'## {record["qid"]}：{record["query"]}', '',
                  '| 阶段 | 候选数 | Top 3：文档 ID / 段落偏移 / 分数 | 耗时（秒） |',
                  '|---|---:|---|---:|']
        for stage, rows in record['stages'].items():
            top = '; '.join(f'{row["metadata"]["doc_id"]} / {row["metadata"]["start"]}:{row["metadata"]["end"]} / {row["score"]:.5f}' for row in rows[:3])
            lines.append(f'| {stage} | {len(rows)} | {top} | {record["timings_seconds"][stage]:.3f} |')
        lines.append('')
    (output / 'STAGES.md').write_text('\n'.join(lines))
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
