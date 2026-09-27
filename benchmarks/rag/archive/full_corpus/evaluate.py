"""Reproducible full-corpus evaluation; search never receives relevance labels.

uv run python benchmarks/rag/archive/full_corpus/evaluate.py --phase all
Public qrels are used only after search, by the scorer and calibration phase.
"""
import argparse
import gzip
import hashlib
import importlib.util
import importlib.metadata
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

import numpy as np
import pyarrow.parquet as pq

import config
from rag.embedding import get_embedding_model
from rag.index import RetrievalIndex, cached_embeddings
from rag.tool import rag_search

ARCHIVE = Path(__file__).resolve().parent
REPORT = ROOT / 'reports/rag/full-corpus'
DATA = ROOT / '.cache/rag/benchmark'


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def load_documents():
    source = next((DATA / 'corpus').glob('corpus-*.parquet'))
    manifest = json.loads((ARCHIVE / 'inputs/queries.json').read_text())
    with source.open('rb') as f:
        assert hashlib.file_digest(f, 'sha256').hexdigest() == manifest['corpus_sha256']
    rows = pq.read_table(source).to_pylist()
    assert len(rows) == manifest['corpus_count']
    return [{'content': row['text'], 'metadata': {
        'doc_id': str(row['id']), 'filename': str(row['id']) + '.txt',
        'source': f'C-MTEB/T2Retrieval:{row["id"]}',
    }} for row in rows]


def load_qrels():
    labels = {}
    for source in sorted((DATA / 'qrels').glob('*.parquet')):
        for row in pq.read_table(source).to_pylist():
            if row['score'] > 0:
                labels.setdefault(str(row['qid']), set()).add(str(row['pid']))
    return labels


def unique_docs(rows):
    return list(dict.fromkeys(row['metadata']['doc_id'] for row in rows))


def metrics(ids, relevant, k=10):
    ids = ids[:k]
    hits = [int(identifier in relevant) for identifier in ids]
    dcg = sum(hit / math.log2(i + 2) for i, hit in enumerate(hits))
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return {'recall@10': sum(hits) / len(relevant) if relevant else 0.,
            'mrr@10': next((1 / (i + 1) for i, hit in enumerate(hits) if hit), 0.),
            'ndcg@10': dcg / ideal if ideal else 0.}


def progress(done, total, computed):
    if done % 5120 == 0 or done == total:
        print(f'embeddings {done}/{total}; new in last batch={computed}', flush=True)


class Baseline:
    """Frozen original splitting/scoring, reusing content vectors for efficiency.

    Only construction is cached; original child scores, threshold and parent
    de-duplication are preserved. Query encoding has NO new instruction prefix.
    """
    def __init__(self, documents):
        path = ARCHIVE / 'baseline/chunking.py'
        spec = importlib.util.spec_from_file_location('legacy_chunking', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        splitter = module.TextSplitter()
        self.children, self.parents = [], []
        for doc in documents:
            for pi, (text, start, end) in enumerate(splitter.split_parents(doc['content'])):
                parent = len(self.parents)
                self.parents.append({'id': f'{doc["metadata"]["doc_id"]}#p{pi}',
                                     'document': text, 'metadata': {**doc['metadata'], 'start': start, 'end': end}})
                for child, _, _ in splitter.split_children(text):
                    self.children.append((parent, child))
        self.model = get_embedding_model()
        self.vectors = cached_embeddings([child for _, child in self.children], self.model, progress)
        self.parent_indexes = np.array([parent for parent, _ in self.children])

    def search(self, query):
        vector = self.model.embed_documents([query])[0]
        scores = self.vectors @ vector
        order = np.argsort(scores)[::-1]  # same ordering as frozen SimpleVectorStore
        rows, seen = [], set()
        for i in order:
            if scores[i] < 0.45:
                break
            parent = int(self.parent_indexes[i])
            if parent in seen:
                continue
            seen.add(parent)
            rows.append({**self.parents[parent], 'score': float(scores[i]), 'rank': len(rows) + 1})
            if len(rows) == 50:
                break
        return rows


def run_baseline(documents, queries, labels):
    begin = time.perf_counter()
    baseline = Baseline(documents)
    build = time.perf_counter() - begin
    output = []
    with gzip.open(REPORT / 'baseline-traces.jsonl.gz', 'wt', encoding='utf-8') as f:
        for query in queries:
            start = time.perf_counter()
            rows = baseline.search(query['text'])
            elapsed = time.perf_counter() - start
            record = {'qid': str(query['id']), 'query': query['text'], 'stages': {'baseline': rows},
                      'seconds': elapsed, 'metrics': metrics(unique_docs(rows), labels.get(str(query['id']), set()))}
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
            output.append(record)
    dump(REPORT / 'baseline-summary.json', {'build_seconds': build, 'queries': len(output),
         'metrics': {key: float(np.mean([row['metrics'][key] for row in output])) for key in output[0]['metrics']},
         'mean_query_seconds': float(np.mean([row['seconds'] for row in output]))})


def run(index, queries, phase, labels):
    output = []
    with gzip.open(REPORT / f'{phase}-traces.jsonl.gz', 'wt', encoding='utf-8') as f:
        for i, query in enumerate(queries):
            trace = {}
            text = rag_search(query['text'], top_k=10, index=index, trace=trace)
            # Real tool string and trace originate in the same production invocation.
            trace.update({'qid': str(query['id']), 'tool_output': text})
            assert all(trace['stages'][name] for name in ('vector', 'rrf', 'rerank'))
            trace['metrics'] = {stage: metrics(unique_docs(rows), labels.get(str(query['id']), set()))
                                for stage, rows in trace['stages'].items()}
            selected = set(trace['selected_ids'])
            trace['metrics']['filtered'] = metrics(unique_docs([
                row for row in trace['stages']['rerank'] if row['id'] in selected]), labels.get(str(query['id']), set()))
            f.write(json.dumps(trace, ensure_ascii=False) + '\n')
            f.flush()
            output.append(trace)
            print(phase, i + 1, '/', len(queries), 'qid', query['id'],
                  'seconds', round(trace['timings_seconds']['total'], 3), flush=True)
    return output


def calibrate(records, labels):
    # Calibrate output gate only. No ranking parameters are tuned on evaluation.
    examples = [(row['score'], row['metadata']['doc_id'] in labels.get(trace['qid'], set()))
                for trace in records for row in trace['stages']['rerank'][:10]]
    thresholds = sorted({round(score, 4) for score, _ in examples})
    def best(beta):
        choices = []
        total = sum(label for _, label in examples)
        for threshold in thresholds:
            selected = [label for score, label in examples if score >= threshold]
            tp = sum(selected)
            precision = tp / len(selected) if selected else 0.
            recall = tp / total if total else 0.
            f = (1 + beta * beta) * precision * recall / (beta * beta * precision + recall) if precision + recall else 0.
            choices.append((f, threshold))
        return max(choices)[1]
    normal = best(1)
    strict = max(best(0.5), normal + 0.1)
    loose = min(best(2), normal - 0.1)
    result = {'strict': strict, 'normal': normal, 'loose': loose,
              'method': 'F0.5/F1/F2 on calibration top-10 parent logits; unjudged treated as nonrelevant; strict ordering enforced by 0.1.',
              'query_ids': [trace['qid'] for trace in records],
              'ranking_parameters': {'candidates': config.RAG_CANDIDATES, 'rerank_top_n': config.RAG_RERANK_TOP_N, 'rrf_k': config.RAG_RRF_K}}
    dump(REPORT / 'calibration.json', result)
    return result


def apply_thresholds(values):
    config.RAG_THRESHOLD_STRICT = values['strict']
    config.RAG_THRESHOLD_NORMAL = values['normal']
    config.RAG_THRESHOLD_LOOSE = values['loose']


def summarize(records, build_seconds):
    stages = records[0]['metrics']
    result = {'queries': len(records), 'index_build_seconds': build_seconds,
              'parents': records[0]['parent_count'], 'children': records[0]['child_count'],
              'metrics': {stage: {key: float(np.mean([trace['metrics'][stage][key] for trace in records]))
                                  for key in records[0]['metrics'][stage]} for stage in stages},
              'timings': {stage: {'mean': float(np.mean([trace['timings_seconds'][stage] for trace in records])),
                                  'p95': float(np.percentile([trace['timings_seconds'][stage] for trace in records], 95))}
                          for stage in records[0]['timings_seconds']}}
    dump(REPORT / 'evaluation-summary.json', result)
    print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=['all', 'index', 'baseline', 'calibration', 'evaluation'], required=True)
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    dump(REPORT / 'execution.json', {
        'embedding_identity': get_embedding_model().identity,
        'embedding_local_path': str(config.EMBEDDING_LOCAL_PATH),
        'reranker': config.RERANK_MODEL,
        'threads': config.RAG_THREADS, 'batch_size': config.RAG_BATCH_SIZE,
        'parent_chars': config.RAG_PARENT_CHARS, 'child_chars': config.RAG_CHILD_CHARS,
        'candidates': config.RAG_CANDIDATES, 'rerank_top_n': config.RAG_RERANK_TOP_N,
        'rrf_k': config.RAG_RRF_K,
        'packages': {name: importlib.metadata.version(name) for name in
                     ('fastembed', 'onnxruntime', 'sentence-transformers', 'torch', 'rank-bm25', 'jieba', 'numpy')},
    })
    queries = json.loads((ARCHIVE / 'inputs/queries.json').read_text())
    documents = load_documents()
    print('full corpus:', len(documents), flush=True)
    labels = load_qrels()
    if args.phase == 'baseline':
        run_baseline(documents, queries['evaluation'], labels)
        return
    start = time.perf_counter()
    index = RetrievalIndex(documents, progress)
    build = time.perf_counter() - start
    print('index ready', len(index.parents), len(index.children), build, flush=True)
    if args.phase == 'index':
        return
    if args.phase in {'all', 'calibration'}:
        records = run(index, queries['calibration'], 'calibration', labels)
        values = calibrate(records, labels)
        if args.phase == 'calibration':
            return
    else:
        values = json.loads((REPORT / 'calibration.json').read_text())
    apply_thresholds(values)
    records = run(index, queries['evaluation'], 'evaluation', labels)
    summarize(records, build)
    if args.phase == 'all':
        del index
        run_baseline(documents, queries['evaluation'], labels)


if __name__ == '__main__':
    main()
