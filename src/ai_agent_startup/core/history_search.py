"""Search archived session summaries without exposing their raw messages."""
from __future__ import annotations

import hashlib
import math

import numpy as np

from ai_agent_startup import config
from ai_agent_startup.rag.cancellation import check_cancelled
from ai_agent_startup.rag.embedding import get_embedding_model, local_model_path
from ai_agent_startup.rag.fusion import reciprocal_rank_fusion
from ai_agent_startup.rag.lexical import BM25Index
from ai_agent_startup.rag.rerank import get_reranker, local_reranker_path
from ai_agent_startup.rag.result_cache import ResultCache


# Each entry contains one normalized summary vector. No raw messages are cached.
_summary_vectors = ResultCache()
_VECTOR_CACHE_SIZE = 512
_VECTOR_BATCH_SIZE = 64
_RESULT_FIELDS = ('ID', 'Time', 'Summary', 'previous_id', 'next_id')


def _local_embedding_available() -> bool:
    path = local_model_path()
    return (config.EMBEDDING_MODEL_SOURCE.upper() == 'LOCAL' and path is not None
            and path.is_dir() and any(path.iterdir()))


def _local_reranker_available() -> bool:
    path = local_reranker_path()
    return path is not None and path.is_dir() and any(path.iterdir())


def _summary_embeddings(model, summaries: list[str]) -> np.ndarray:
    identity = model.identity
    values: list[np.ndarray | None] = [None] * len(summaries)
    missing: list[int] = []
    keys = [(identity, hashlib.sha256(summary.encode('utf-8')).digest())
            for summary in summaries]
    for index, key in enumerate(keys):
        check_cancelled()
        cached = _summary_vectors.get(key, _VECTOR_CACHE_SIZE)
        if cached is None:
            missing.append(index)
        else:
            values[index] = cached
    if missing:
        computed = np.asarray(model.embed_documents([summaries[index] for index in missing]), dtype=np.float32)
        if (computed.ndim != 2 or len(computed) != len(missing)
                or not np.isfinite(computed).all()):
            raise ValueError('历史摘要 embedding 形状或数值无效')
        for index, vector in zip(missing, computed, strict=True):
            if not np.any(vector):
                raise ValueError('历史摘要 embedding 为零向量')
            values[index] = vector
            _summary_vectors.put(keys[index], vector, _VECTOR_CACHE_SIZE)
    return np.stack(values)


def _vector_ranking(chunks: list[dict], query: str, limit: int) -> list[dict]:
    model = get_embedding_model()
    query_vector = np.asarray(model.embed_queries([query]), dtype=np.float32)
    if (query_vector.ndim != 2 or query_vector.shape[0] != 1 or
            not np.isfinite(query_vector).all() or not np.any(query_vector)):
        raise ValueError('历史查询 embedding 形状或数值无效')
    scores = np.empty(len(chunks), dtype=np.float32)
    for start in range(0, len(chunks), _VECTOR_BATCH_SIZE):
        check_cancelled()
        summaries = [row['Summary'] for row in chunks[start:start + _VECTOR_BATCH_SIZE]]
        vectors = _summary_embeddings(model, summaries)
        if vectors.shape[1] != query_vector.shape[1]:
            raise ValueError('历史摘要 embedding 维度与查询不匹配')
        scores[start:start + len(vectors)] = vectors @ query_vector[0]
    if not np.isfinite(scores).all():
        raise ValueError('历史向量分数无效')
    order = sorted(range(len(chunks)), key=lambda index: (-float(scores[index]), chunks[index]['ID']))
    return [{'id': chunks[index]['ID'], 'score': float(scores[index])}
            for index in order[:limit]]


def search_chunks(chunks: list[dict], query: str, limit: int = 5,
                  method: str = 'bm25') -> list[dict]:
    """Rank summaries with real stage scores; never return raw history fields.

    Hybrid uses existing local model artifacts only. If they are unavailable,
    retrieval falls back to BM25 and says which stages did not run.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query 不能为空')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('limit 必须在 1..20 之间')
    if method not in {'bm25', 'hybrid'}:
        raise ValueError('method 必须是 bm25 或 hybrid')
    if not chunks:
        return []
    query = query.strip()
    check_cancelled()
    summaries = [row['Summary'] for row in chunks]
    if not all(isinstance(summary, str) for summary in summaries):
        raise ValueError('历史摘要必须是文本')
    if len({row['ID'] for row in chunks}) != len(chunks):
        raise ValueError('历史摘要 ID 不得重复')
    budget = min(len(chunks), max(limit, config.RAG_CANDIDATES))
    bm25 = [{'id': chunks[index]['ID'], 'score': score}
            for index, score in BM25Index(summaries).search(query, budget)]
    stages = {'bm25': bm25, 'vector': [], 'rrf': [], 'rerank': []}
    selected = bm25[:limit]
    effective = 'bm25'
    degraded_reason = None

    if method == 'hybrid':
        if not _local_embedding_available():
            degraded_reason = 'local embedding model is unavailable'
        else:
            try:
                check_cancelled()
                stages['vector'] = _vector_ranking(chunks, query, budget)
            except (InterruptedError, KeyboardInterrupt):
                raise
            except Exception as exc:
                degraded_reason = f'embedding unavailable: {type(exc).__name__}'
            else:
                stages['rrf'] = reciprocal_rank_fusion(
                    [stages['vector'], bm25], config.RAG_RRF_K)
                selected = stages['rrf'][:limit]
                effective = 'rrf'
                if not _local_reranker_available():
                    degraded_reason = 'local reranker model is unavailable'
                else:
                    candidates = stages['rrf'][:max(limit, config.RAG_RERANK_TOP_N)]
                    by_id = {row['ID']: row for row in chunks}
                    try:
                        check_cancelled()
                        scores = get_reranker().score(
                            query, [by_id[row['id']]['Summary'] for row in candidates])
                        if (len(scores) != len(candidates)
                                or not all(math.isfinite(score) for score in scores)):
                            raise ValueError('历史重排分数无效')
                    except (InterruptedError, KeyboardInterrupt):
                        raise
                    except Exception as exc:
                        degraded_reason = f'reranker unavailable: {type(exc).__name__}'
                    else:
                        stages['rerank'] = sorted(
                            ({'id': row['id'], 'score': float(score), 'rrf_rank': rank}
                             for rank, (row, score) in enumerate(zip(candidates, scores, strict=True), 1)),
                            key=lambda row: (-row['score'], row['rrf_rank'], row['id']),
                        )
                        selected = stages['rerank'][:limit]
                        effective = 'hybrid'

    stage_scores = {name: {row['id']: row['score'] for row in rows}
                    for name, rows in stages.items()}
    by_id = {row['ID']: row for row in chunks}
    output = []
    for hit in selected:
        source = by_id[hit['id']]
        row = {name: source[name] for name in _RESULT_FIELDS if name in source}
        score = float(hit['score'])
        row['score'] = score
        row['retrieval'] = {
            'requested_method': method,
            'effective_method': effective,
            'bm25_score': stage_scores['bm25'].get(hit['id']),
            'vector_score': stage_scores['vector'].get(hit['id']),
            'rrf_score': stage_scores['rrf'].get(hit['id']),
            'rerank_score': stage_scores['rerank'].get(hit['id']),
        }
        if degraded_reason:
            row['retrieval']['degraded_reason'] = degraded_reason
        output.append(row)
    return output
