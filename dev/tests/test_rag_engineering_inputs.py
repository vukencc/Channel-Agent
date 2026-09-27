"""Integrity checks for engineering inputs, independent of models and archives."""
import hashlib
import json

import pytest

from dev.rag.dataset import load_inputs


@pytest.fixture
def inputs(tmp_path):
    corpus = tmp_path / 'documents.jsonl'
    corpus.write_text(json.dumps({'content': 'input', 'metadata': {'doc_id': 'a'}}) + '\n')
    manifest = tmp_path / 'manifest.json'
    values = {'documents_sha256': hashlib.sha256(corpus.read_bytes()).hexdigest(),
              'document_ids': ['a'], 'queries': [{'id': 'q', 'text': 'query'}]}
    manifest.write_text(json.dumps(values))
    return manifest, corpus, values


def test_load_inputs_preserves_frozen_documents_and_queries(inputs):
    manifest, corpus, values = inputs
    documents, queries = load_inputs(manifest, corpus)
    assert documents[0]['metadata']['doc_id'] == 'a'
    assert queries == values['queries']


def test_modified_corpus_is_rejected(inputs):
    manifest, corpus, _ = inputs
    corpus.write_text(corpus.read_text().replace('input', 'changed'))
    with pytest.raises(ValueError, match='checksum'):
        load_inputs(manifest, corpus)


def test_manifest_id_mismatch_is_rejected(inputs):
    manifest, corpus, values = inputs
    values['document_ids'] = ['wrong']
    manifest.write_text(json.dumps(values))
    with pytest.raises(ValueError, match='IDs'):
        load_inputs(manifest, corpus)


def test_missing_subset_has_preparation_instruction(inputs):
    manifest, corpus, _ = inputs
    with pytest.raises(FileNotFoundError, match='prepare_engineering'):
        load_inputs(manifest, corpus.with_name('missing.jsonl'))
