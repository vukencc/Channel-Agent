"""固定公开标注候选池上的小规模校准；输出不覆盖输入或默认阈值。"""
import argparse
import hashlib
import json
from pathlib import Path


def metrics(rows, threshold=float('-inf'), k=10):
    totals = {'hit@k': 0., 'MRR': 0., 'precision': 0., 'recall': 0.}
    for row in rows:
        ranked = list(dict.fromkeys(identifier for identifier, score in row['ranking'] if score >= threshold))[:k]
        relevant = set(row['relevant'])
        hits = [i + 1 for i, identifier in enumerate(ranked) if identifier in relevant]
        totals['hit@k'] += bool(hits)
        totals['MRR'] += 1 / hits[0] if hits else 0
        totals['precision'] += len(hits) / len(ranked) if ranked else 0
        totals['recall'] += len(hits) / len(relevant) if relevant else 0
    return {key: value / len(rows) if rows else 0 for key, value in totals.items()}


def calibrate(rows):
    selected = [row for row in rows if row['split'] == 'calibration']
    if not selected:
        raise ValueError('必须有独立 calibration 标注')
    scores = sorted({score for row in selected for _, score in row['ranking']})
    if not scores:
        raise ValueError('校准候选为空')
    candidates = [scores[0] - 1, *scores]
    def quality(threshold):
        result = metrics(selected, threshold)
        p, r = result['precision'], result['recall']
        return (2 * p * r / (p + r) if p + r else 0, p, threshold)
    return max(candidates, key=quality)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path('dev/rag/inputs/quality.json'))
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for source in (args.manifest, args.corpus):
        if source.resolve().is_relative_to(args.output.resolve()):
            parser.error('输出目录不能包含输入')
    import pyarrow.parquet as pq
    from ai_agent_startup import config
    from ai_agent_startup.rag.index import RetrievalIndex
    from ai_agent_startup.rag.tool import rag_search
    from ai_agent_startup.rag.embedding import get_embedding_model
    manifest = json.loads(args.manifest.read_text())
    with args.corpus.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['corpus_sha256']:
            raise ValueError('公开语料校验值不匹配')
    wanted = set(manifest['document_ids'])
    documents = []
    for batch in pq.ParquetFile(args.corpus).iter_batches(columns=['id', 'text']):
        for row in batch.to_pylist():
            if str(row['id']) in wanted:
                documents.append({'content': row['text'], 'metadata': {
                    'doc_id': str(row['id']), 'filename': str(row['id']) + '.txt', 'source': 'C-MTEB/T2Retrieval'}})
    if {d['metadata']['doc_id'] for d in documents} != wanted:
        raise ValueError('候选池有缺失文档')
    documents.sort(key=lambda d: d['metadata']['doc_id'])
    args.output.mkdir(parents=True, exist_ok=False)
    config.RAG_CACHE_DIR = args.output / 'cache'
    index = RetrievalIndex(documents)
    rows, traces = [], []
    for query in manifest['queries']:
        trace = {}
        rag_search(query['text'], top_k=10, index=index, trace=trace)
        traces.append({'id': query['id'], **trace})
        rows.append({**query, 'ranking': [(r['metadata']['doc_id'], r['score']) for r in trace['stages']['rerank']]})
        print('完成', query['id'], flush=True)
    threshold = calibrate(rows)
    evaluation = [row for row in rows if row['split'] == 'evaluation']
    summary = {'purpose': manifest['selection'], 'documents': len(documents), 'queries': len(rows),
               'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
               'embedding_identity': get_embedding_model().identity, 'reranker': str(config.RERANK_LOCAL_PATH or config.RERANK_MODEL),
               'suggested_normal_threshold': threshold, 'default_threshold': config.RAG_THRESHOLD_NORMAL,
               'evaluation_default': metrics(evaluation, config.RAG_THRESHOLD_NORMAL),
               'evaluation_suggested': metrics(evaluation, threshold),
               'stages': {stage: metrics([{**query, 'ranking': [(r['metadata']['doc_id'], r['score']) for r in trace['stages'][stage]]}
                           for query, trace in zip(manifest['queries'], traces) if query['split'] == 'evaluation'])
                          for stage in ('vector', 'bm25', 'rrf', 'rerank')}}
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'traces.jsonl').write_text(''.join(json.dumps(trace, ensure_ascii=False) + '\n' for trace in traces))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
