"""Verify offline metric arithmetic independently of model quality."""
import math

import pytest

from dev.rag.archive.full_corpus.evaluate import metrics, unique_docs


def test_document_deduplication_preserves_first_rank():
    rows = [{'metadata': {'doc_id': value}} for value in ['a', 'a', 'b', 'a', 'c']]
    assert unique_docs(rows) == ['a', 'b', 'c']


def test_known_recall_reciprocal_rank_and_ndcg():
    result = metrics(['miss', 'hit'], {'hit'})
    assert result['recall@10'] == 1
    assert result['mrr@10'] == 0.5
    assert result['ndcg@10'] == pytest.approx(1 / math.log2(3))


def test_metric_cutoff_and_missing_judgments():
    assert metrics(['miss'] * 10 + ['hit'], {'hit'}) == {
        'recall@10': 0, 'mrr@10': 0, 'ndcg@10': 0,
    }
    assert metrics([], set()) == {'recall@10': 0, 'mrr@10': 0, 'ndcg@10': 0}
    result = metrics(['a'], {'a', 'b'})
    assert result['recall@10'] == 0.5
    assert result['ndcg@10'] == pytest.approx(1 / (1 + 1 / math.log2(3)))
