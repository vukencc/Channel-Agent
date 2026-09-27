"""Check all public-corpus spans without models or relevance labels."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
(ROOT / '.cache/reports/rag/full-corpus').mkdir(parents=True, exist_ok=True)

import pyarrow.parquet as pq
from rag.chunking import TextSplitter

source = next((ROOT / '.cache/rag/benchmark/corpus').glob('corpus-*.parquet'))
splitter = TextSplitter()
counts = {'documents': 0, 'parents': 0, 'children': 0, 'empty_documents': 0}
compact = lambda text: ''.join(text.split())
for batch in pq.ParquetFile(source).iter_batches(batch_size=1024):
    for document in batch.to_pylist():
        text = document['text']
        parents = splitter.split_parents(text)
        assert compact(''.join(piece for piece, _, _ in parents)) == compact(text)
        counts['documents'] += 1
        counts['empty_documents'] += int(not parents)
        for parent, start, end in parents:
            assert text[start:end] == parent and len(parent) <= splitter.parent_chars
            children = splitter.pack_children(parent)
            assert compact(''.join(piece for piece, _, _ in children)) == compact(parent)
            for child, a, b in children:
                assert text[start + a:start + b] == child and len(child) <= splitter.child_chars
            counts['parents'] += 1
            counts['children'] += len(children)
with source.open('rb') as stream:
    counts['corpus_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
counts['result'] = 'Every parent/child offset matches original text; no non-whitespace content omitted.'
(ROOT / '.cache/reports/rag/full-corpus/structure-audit.json').write_text(json.dumps(counts, indent=2) + '\n')
print(json.dumps(counts, indent=2))
