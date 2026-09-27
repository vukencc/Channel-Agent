"""Real local cross-encoder inference; load/inference errors are never hidden."""
from functools import lru_cache
from rag.cancellation import check_cancelled
import json

import numpy as np

import config


class Reranker:
    def __init__(self):
        import torch
        from sentence_transformers import CrossEncoder
        torch.set_num_threads(config.RAG_THREADS)
        path = local_reranker_path()
        self.model = CrossEncoder(
            str(path) if path else config.RERANK_MODEL,
            revision=config.RERANK_REVISION if not path else None,
            device='cpu', max_length=512,
            cache_folder=str(config.RAG_CACHE_DIR / 'models'),
            local_files_only=bool(path), trust_remote_code=False,
        )
        self.identity = str(path or config.RERANK_MODEL)

    def score(self, query: str, passages: list[str]) -> list[float]:
        import torch
        if not passages:
            return []
        tokenizer = self.model.tokenizer
        # Reserve query tokens; window the entire passage so its tail is scored.
        query_ids = tokenizer.encode(query, add_special_tokens=False)[:96]
        bounded_query = tokenizer.decode(query_ids, skip_special_tokens=True)
        window = 512 - len(query_ids) - tokenizer.num_special_tokens_to_add(pair=True)
        pairs, owners = [], []
        for index, passage in enumerate(passages):
            check_cancelled()
            tokens = tokenizer.encode(passage, add_special_tokens=False)
            for start in range(0, max(1, len(tokens)), window):
                pairs.append((bounded_query, tokenizer.decode(tokens[start:start + window], skip_special_tokens=True)))
                owners.append(index)
        batches = []
        for start in range(0, len(pairs), config.RAG_BATCH_SIZE):
            check_cancelled()
            batches.append(np.asarray(self.model.predict(
                pairs[start:start + config.RAG_BATCH_SIZE], batch_size=config.RAG_BATCH_SIZE,
                activation_fn=torch.nn.Identity(), show_progress_bar=False,
            )).reshape(-1))
        scores = np.concatenate(batches)
        if len(scores) != len(pairs) or not np.isfinite(scores).all():
            raise ValueError('Reranker returned invalid scores')
        output = np.full(len(passages), -np.inf)
        for owner, score in zip(owners, scores, strict=True):
            output[owner] = max(output[owner], float(score))
        return output.tolist()


@lru_cache(maxsize=1)
def _get_reranker(key):
    return Reranker()


def get_reranker():
    from rag.embedding import artifact_signature
    return _get_reranker((config.RERANK_MODEL, config.RERANK_REVISION,
                          config.RERANK_LOCAL_PATH, config.RAG_CACHE_DIR,
                          config.RAG_THREADS, artifact_signature(local_reranker_path())))


def local_reranker_path():
    if config.RERANK_LOCAL_PATH:
        return config.RERANK_LOCAL_PATH
    path = config.RAG_CACHE_DIR / 'reranker'
    metadata = path / 'source.json'
    if metadata.is_file():
        source = json.loads(metadata.read_text())
        if source['repo'] == config.RERANK_MODEL and (
                not config.RERANK_REVISION or source['revision'] == config.RERANK_REVISION):
            return path
    return None
