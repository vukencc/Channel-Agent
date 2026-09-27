"""Extract frozen small inputs once; subsequent runs need only the JSONL subset."""
import argparse
import hashlib
import json
from pathlib import Path

from benchmarks.rag.dataset import DEFAULT_CORPUS, DEFAULT_MANIFEST, ROOT


def prepare(source: Path, manifest_path: Path, output: Path) -> None:
    import pyarrow.parquet as pq

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    with source.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['corpus_sha256']:
            raise ValueError('Public source checksum differs from frozen input')
    wanted = set(manifest['document_ids'])
    found = {}
    for batch in pq.ParquetFile(source).iter_batches(columns=['id', 'text']):
        for row in batch.to_pylist():
            identifier = str(row['id'])
            if identifier in wanted:
                if identifier in found:
                    raise ValueError('Duplicate source document ID')
                found[identifier] = {'content': row['text'], 'metadata': {
                    'doc_id': identifier, 'filename': identifier + '.txt',
                    'source': 'C-MTEB/T2Retrieval:' + identifier}}
    if set(found) != wanted:
        raise ValueError('Frozen documents missing from public source')
    content = ''.join(json.dumps(found[key], ensure_ascii=False) + '\n'
                      for key in manifest['document_ids']).encode('utf-8')
    if hashlib.sha256(content).hexdigest() != manifest['documents_sha256']:
        raise ValueError('Extracted corpus checksum differs from frozen input')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    temporary.write_bytes(content)
    temporary.replace(output)
    print(f'Prepared {len(found)} documents: {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--output', type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()
    source = args.source or next((ROOT / '.cache/rag/benchmark/corpus').glob('corpus-*.parquet'), None)
    if source is None:
        parser.error('Download corpus with scripts/rag/prepare_assets.py --only corpus, or supply --source')
    prepare(source, args.manifest, args.output)


if __name__ == '__main__':
    main()
