"""Engineering inputs only: no model, scoring, calibration or report imports."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / 'benchmarks/rag/inputs/engineering.json'
DEFAULT_CORPUS = ROOT / '.cache/rag/benchmark/engineering/documents.jsonl'


def load_inputs(manifest_path: Path, corpus_path: Path) -> tuple[list[dict], list[dict]]:
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not corpus_path.is_file():
        raise FileNotFoundError('Prepare the small corpus with python -m benchmarks.rag.prepare_engineering')
    raw = corpus_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['documents_sha256']:
        raise ValueError('Engineering corpus checksum differs from frozen input')
    documents = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    ids = [doc['metadata']['doc_id'] for doc in documents]
    if ids != manifest['document_ids'] or len(set(ids)) != len(ids):
        raise ValueError('Engineering document IDs differ from frozen input')
    return documents, manifest['queries']
