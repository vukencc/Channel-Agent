"""Vector + BM25 -> parent RRF -> real cross-encoder -> readable tool result."""
from time import perf_counter

from ai_agent_startup import config
from ai_agent_startup.rag.cancellation import check_cancelled
from ai_agent_startup.rag.fusion import reciprocal_rank_fusion
from ai_agent_startup.rag.index import DocLoader, RetrievalIndex, get_index
from ai_agent_startup.rag.rerank import get_reranker

_BREADTH_TOP_K = {'narrow': 2, 'normal': 4, 'wide': 8}


def resolve_params(strictness: str = 'normal', breadth: str = 'normal', thresholds=None) -> tuple[float, int]:
    thresholds = thresholds if thresholds is not None else {'strict': config.RAG_THRESHOLD_STRICT, 'normal': config.RAG_THRESHOLD_NORMAL,
                  'loose': config.RAG_THRESHOLD_LOOSE}
    if strictness not in thresholds or breadth not in _BREADTH_TOP_K:
        raise ValueError('strictness must be strict/normal/loose; breadth must be narrow/normal/wide')
    if not thresholds['strict'] > thresholds['normal'] > thresholds['loose']:
        raise ValueError('Rerank thresholds must satisfy strict > normal > loose')
    return thresholds[strictness], _BREADTH_TOP_K[breadth]


def rag_search(query: str, top_k: int | None = None, strictness: str = 'normal',
               breadth: str = 'normal', *, index: RetrievalIndex | None = None,
               trace: dict | None = None, updated_after: float | None = None,
               thresholds: dict | None = None) -> str:
    """Run every stage; optional structured trace uses the exact production path.

    Scores from different stages are intentionally not interchangeable. The
    strictness gate applies only after reranking; raw stage ranks remain visible.
    """
    threshold, limit = resolve_params(strictness, breadth, thresholds)
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must contain non-whitespace text')
    query = query.strip()
    if top_k is not None:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError('top_k must be a positive integer')
        limit = top_k
    if min(config.RAG_CANDIDATES, config.RAG_RERANK_TOP_N, config.RAG_RRF_K) < 1:
        raise ValueError('Candidate budgets and RRF k must be positive')
    check_cancelled()
    started = perf_counter()
    index = (get_index(track_updates=True) if updated_after is not None else get_index()) if index is None else index
    allowed = ({i for i, row in enumerate(index.parents) if row['metadata'].get('updated_at', float('-inf')) > updated_after}
               if updated_after is not None else None)
    times = {'index': perf_counter() - started}
    stages = {'vector': [], 'bm25': [], 'rrf': [], 'rerank': []}
    scores = {}
    if index.parents and (allowed is None or allowed):
        check_cancelled()
        then = perf_counter()
        search_options = {'allowed': allowed} if allowed is not None else {}
        stages['vector'] = index.dense(query, max(config.RAG_CANDIDATES, limit), **search_options)
        times['vector'] = perf_counter() - then
        check_cancelled()
        then = perf_counter()
        stages['bm25'] = index.bm25(query, max(config.RAG_CANDIDATES, limit), **search_options)
        times['bm25'] = perf_counter() - then
        check_cancelled()
        then = perf_counter()
        stages['rrf'] = reciprocal_rank_fusion([stages['vector'], stages['bm25']], config.RAG_RRF_K)
        times['rrf'] = perf_counter() - then
        budget = config.RAG_RERANK_BY_BREADTH.get(breadth, config.RAG_RERANK_TOP_N)
        candidates = stages['rrf'][:max(budget, limit)]
        check_cancelled()
        then = perf_counter()
        values = get_reranker().score(query, [index.by_id[row['id']]['document'] for row in candidates])
        stages['rerank'] = sorted(
            [{'id': row['id'], 'score': score, 'rrf_rank': i + 1}
             for i, (row, score) in enumerate(zip(candidates, values, strict=True))],
            key=lambda row: (-row['score'], row['rrf_rank'], row['id']),
        )
        times['rerank'] = perf_counter() - then
        scores = {name: {row['id']: row['score'] for row in rows} for name, rows in stages.items()}
    selected = [row for row in stages['rerank'] if row['score'] >= threshold][:limit]
    times['total'] = perf_counter() - started
    if trace is not None:
        trace.clear()
        trace.update({
            'query': query, 'threshold': threshold, 'strictness': strictness,
            'parent_count': len(index.parents), 'child_count': len(index.children),
            'vector_backend': getattr(index, 'vector_backend', 'exact'),
            'timings_seconds': times,
            'stages': {name: [{**index.by_id[row['id']], **row, 'rank': rank}
                              for rank, row in enumerate(rows, 1)] for name, rows in stages.items()},
            'selected_ids': [row['id'] for row in selected],
        })
        if allowed is not None:
            trace['filter'] = {'updated_after': updated_after, 'eligible_parents': len(allowed)}
    status = (f'[检索状态] strictness={strictness}（重排 logit 阈值 {threshold:.2f}，非概率）'
              f' / breadth={breadth}（最多 {limit} 条）\n'
              f'向量 {len(stages["vector"])} / BM25 {len(stages["bm25"])} / '
              f'RRF {len(stages["rrf"])} / 重排 {len(stages["rerank"])}\n'
              f'命中 {len(selected)}/{len(index.parents)} 个段落')
    if not index.parents:
        return status + '：知识库为空，请检查 DOC_DIR 下的 .txt / .md 文件。'
    if allowed is not None and not allowed:
        return status + '：没有符合修改时间条件的段落。'
    if not selected:
        return status + '：没有候选达到重排阈值；可放宽 strictness 或改写 query 后重试。'
    body = []
    for number, row in enumerate(selected, 1):
        parent = index.by_id[row['id']]
        metadata = parent['metadata']
        detail = ' / '.join(f'{name}={scores[name][row["id"]]:.4f}'
                            if row['id'] in scores[name] else f'{name}=未召回'
                            for name in stages)
        body.append(f'[{number}] 来源: {metadata["filename"]} '
                    f'({metadata["start"]}:{metadata["end"]}; {detail})\n{parent["document"]}')
    return status + '\n\n' + '\n\n'.join(body)
