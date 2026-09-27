"""Download pinned public assets and freeze queries before running retrieval.

Run from the repository root. No relevance labels enter the search corpus.
"""
import argparse
import fcntl
import hashlib
import json
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.cache/rag/benchmark'
REPORT = ROOT / 'reports/rag'
MODEL = ROOT / '.cache/rag/reranker'
REPOS = {
    'corpus': ('datasets', 'C-MTEB/T2Retrieval'),
    'qrels': ('datasets', 'C-MTEB/T2Retrieval-qrels'),
    'embedding': ('models', 'Qdrant/bge-small-zh-v1.5'),
    'reranker': ('models', 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1'),
}


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def download(client, url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix(target.suffix + '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _download(client, url, target)


def _download(client, url, target):
    if target.exists():
        return
    if target.suffix in {'.parquet', '.safetensors'}:
        return download_ranges(client, url, target)
    for attempt in range(20):
        try:
            temporary = target.with_suffix(target.suffix + '.part')
            offset = temporary.stat().st_size if temporary.exists() else 0
            # Fresh resolver request prevents stale signed redirects on retry.
            with client.stream('GET', url + f'?download=true&offset={offset}',
                               headers={'Range': f'bytes={offset}-'} if offset else {}) as response:
                response.raise_for_status()
                append = offset and response.status_code == 206
                if append and not response.headers.get('content-range', '').startswith(f'bytes {offset}-'):
                    raise RuntimeError('Unexpected range response; refusing corrupt download')
                print('transfer', target.name, 'resume', offset if append else 0, flush=True)
                with temporary.open('ab' if append else 'wb') as f:
                    for block in response.iter_bytes(1024 * 1024):
                        f.write(block)
            temporary.replace(target)
            print('downloaded', target.name, target.stat().st_size, flush=True)
            return
        except httpx.HTTPError as exc:
            print('retry', target.name, type(exc).__name__, flush=True)
            if attempt == 19:
                raise
            time.sleep(2)


def download_ranges(client, url, target):
    """Bounded parallel ranges with durable segment checkpoints."""
    response = client.get(url + '?range=probe', headers={'Range': 'bytes=0-0'})
    response.raise_for_status()
    if response.status_code != 206:
        raise RuntimeError('Server does not support range downloads')
    total = int(response.headers['content-range'].split('/')[-1])
    temporary = target.with_suffix(target.suffix + '.part')
    parts = target.parent / (target.name + '.segments')
    parts.mkdir(exist_ok=True)
    plan_path = parts / 'plan.json'
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        assert plan['total'] == total
        prefix = plan['prefix']
    else:
        prefix = temporary.stat().st_size if temporary.exists() else 0
        plan_path.write_text(json.dumps({'total': total, 'prefix': prefix}))
    ranges = [(start, min(start + 4 * 1024 * 1024, total) - 1)
              for start in range(prefix, total, 4 * 1024 * 1024)]
    def fetch(bounds):
        start, end = bounds
        piece = parts / str(start)
        if piece.exists() and piece.stat().st_size == end - start + 1:
            return
        for attempt in range(12):
            try:
                r = client.get(url + f'?segment={start}&attempt={attempt}',
                               headers={'Range': f'bytes={start}-{end}'}, timeout=60)
                r.raise_for_status()
                if r.headers.get('content-range') != f'bytes {start}-{end}/{total}' or len(r.content) != end - start + 1:
                    raise RuntimeError('Invalid download segment')
                piece.write_bytes(r.content)
                return
            except httpx.HTTPError as exc:
                print('segment retry', start, attempt + 1, type(exc).__name__, flush=True)
                if attempt == 11:
                    raise
                time.sleep(1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, pair) for pair in ranges]
        for count, future in enumerate(as_completed(futures), 1):
            future.result()
            if count % 5 == 0 or count == len(futures):
                print('segments', target.name, count, '/', len(futures), flush=True)
    with temporary.open('r+b' if temporary.exists() else 'wb') as f:
        f.truncate(prefix)
        f.seek(prefix)
        for start, _ in ranges:
            f.write((parts / str(start)).read_bytes())
    assert temporary.stat().st_size == total
    temporary.replace(target)
    print('downloaded', target.name, total, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--endpoint', default='https://huggingface.co')
    parser.add_argument('--only', choices=['reranker', 'embedding', 'corpus', 'qrels'])
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--onnx-wheel', action='store_true')
    args = parser.parse_args()
    if args.onnx_wheel:
        lock = tomllib.loads((ROOT / 'uv.lock').read_text())
        package = next(p for p in lock['package'] if p['name'] == 'onnx')
        wheel = next(w for w in package['wheels'] if 'cp312-abi3-manylinux' in w['url'] and 'x86_64' in w['url'])
        target = DATA / 'wheels' / wheel['url'].rsplit('/', 1)[-1]
        target.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            download_ranges(client, wheel['url'], target)
        assert sha(target) == wheel['hash'].removeprefix('sha256:')
        print('verified wheel', target, flush=True)
        return
    DATA.mkdir(parents=True, exist_ok=True)
    REPORT.mkdir(parents=True, exist_ok=True)
    assets_path = REPORT / ('assets-' + args.only + '.json' if args.only else 'assets.json')
    assets = json.loads(assets_path.read_text()) if assets_path.exists() else {}
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for key, (kind, repo) in REPOS.items():
            if args.only and key != args.only:
                continue
            info = assets.get(key)
            if info is None and key == 'embedding':
                info = {
                    'repo': repo, 'revision': '46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59',
                    'files': ['config.json', 'model_optimized.onnx', 'special_tokens_map.json',
                              'tokenizer.json', 'tokenizer_config.json'],
                    'sha256': {'model_optimized.onnx': '1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38'},
                }
                assets[key] = info
            if info is None:
                response = client.get(f'{args.endpoint}/api/{kind}/{repo}')
                response.raise_for_status()
                metadata = response.json()
                files = [v['rfilename'] for v in metadata['siblings']]
                if kind == 'datasets':
                    files = [f for f in files if f.endswith('.parquet')]
                else:
                    files = [f for f in files if f in {
                        'config.json', 'model.safetensors', 'tokenizer.json',
                        'tokenizer_config.json', 'special_tokens_map.json',
                        'sentencepiece.bpe.model',
                    }]
                info = {'repo': repo, 'revision': metadata['sha'], 'files': files}
                assets[key] = info
                assets_path.write_text(json.dumps(assets, indent=2) + '\n')
            destination = MODEL if key == 'reranker' else DATA / key
            if key == 'embedding':
                destination = ROOT / '.cache/rag/models/models--Qdrant--bge-small-zh-v1.5/snapshots' / info['revision']
            for name in info['files']:
                if args.probe and key == 'reranker' and name != 'model.safetensors':
                    continue
                prefix = 'datasets/' if kind == 'datasets' else ''
                target = destination / Path(name).name
                if args.probe:
                    begin = time.monotonic()
                    url = f'{args.endpoint}/{prefix}{repo}/resolve/{info["revision"]}/{name}'
                    with client.stream('GET', url, headers={'Range': 'bytes=0-1048575'}) as response:
                        print(response.status_code, response.headers.get('content-range'), flush=True)
                        size = 0
                        for block in response.iter_bytes(65536):
                            size += len(block)
                            if size >= 1048576:
                                break
                    print('probe', name, size, round(time.monotonic() - begin, 2), flush=True)
                    return
                download(client, f'{args.endpoint}/{prefix}{repo}/resolve/{info["revision"]}/{name}', target)
                digest = sha(target)
                expected = info.setdefault('sha256', {}).get(name)
                if expected and expected != digest:
                    raise RuntimeError(f'Checksum mismatch: {target}')
                info['sha256'][name] = digest
            assets_path.write_text(json.dumps(assets, indent=2) + '\n')
            if key == 'reranker':
                (MODEL / 'source.json').write_text(json.dumps(info, indent=2) + '\n')
            if key == 'qrels':
                freeze_queries()
    if not args.only:
        freeze_queries()


def freeze_queries():
    query_file = next((DATA / 'corpus').glob('queries-*.parquet'))
    corpus_file = next((DATA / 'corpus').glob('corpus-*.parquet'))
    queries = pq.read_table(query_file).to_pylist()
    ordered = sorted(queries, key=lambda row: hashlib.sha256(str(row['id']).encode()).hexdigest())
    manifest = {
        'selection': 'Sort all official query IDs by SHA-256 UTF-8 hex; first 50 calibration, next 100 evaluation. No label-based selection.',
        'corpus_count': pq.read_metadata(corpus_file).num_rows,
        'query_count': len(queries),
        'corpus_sha256': sha(corpus_file),
        'queries_sha256': sha(query_file),
        'calibration': ordered[:50],
        'evaluation': ordered[50:150],
    }
    target = REPORT / 'queries.json'
    content = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    if target.exists() and target.read_text() != content:
        raise RuntimeError('Frozen query manifest differs; refusing to replace it')
    target.write_text(content)
    print('frozen queries:', len(queries), 'corpus:', manifest['corpus_count'], flush=True)


if __name__ == '__main__':
    main()
