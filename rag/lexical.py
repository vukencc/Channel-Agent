"""Chinese/Latin tokenization and BM25, retaining genuine lexical matches only."""
from functools import lru_cache
import re
import math
import unicodedata

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

_TOKENIZER = jieba.Tokenizer()
_TOKENIZER.tmp_dir = '/tmp'


@lru_cache(maxsize=8192)
def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize('NFKC', text).casefold()
    return [token for token in _TOKENIZER.cut(normalized, HMM=False)
            if re.search(r'\w', token, flags=re.UNICODE)]


class BM25Index:
    def __init__(self, texts: list[str]):
        tokens = [tokenize(text) for text in texts]
        self.postings: dict[str, list[int]] = {}
        for index, row in enumerate(tokens):
            for token in set(row):
                self.postings.setdefault(token, []).append(index)
        self.model = BM25Okapi(tokens) if any(tokens) else None
        if self.model is not None:
            # Lucene-style positive IDF avoids negative common-word weights in
            # tiny corpora. TF saturation still uses rank_bm25's k1=1.5, b=.75.
            self.model.idf = {token: math.log1p((len(texts) - len(rows) + 0.5) / (len(rows) + 0.5))
                              for token, rows in self.postings.items()}

    def search(self, query: str, limit: int) -> list[tuple[int, float]]:
        if limit < 1:
            raise ValueError('limit must be positive')
        tokens = list(dict.fromkeys(tokenize(query)))
        if not tokens or self.model is None:
            return []
        # Membership in postings determines a lexical match; punctuation-only
        # inputs and missing words must never contribute arbitrary zero-score IDs.
        indexes = sorted({i for token in tokens for i in self.postings.get(token, ())})
        if not indexes:
            return []
        scores = self.model.get_batch_scores(tokens, indexes)
        order = np.lexsort((np.asarray(indexes), -np.asarray(scores)))[:limit]
        return [(indexes[i], float(scores[i])) for i in order]
