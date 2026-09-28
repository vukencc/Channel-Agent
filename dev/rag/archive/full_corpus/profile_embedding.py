"""Compare FP32/INT8 on fixed real corpus spans before choosing runtime settings."""
import json
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
(ROOT / '.cache/reports/rag/full-corpus').mkdir(parents=True, exist_ok=True)

import numpy as np
import pyarrow.parquet as pq

from ai_agent_startup import config
from ai_agent_startup.rag.chunking import TextSplitter
from ai_agent_startup.rag.embedding import get_embedding_model

splitter = TextSplitter()
source = next((ROOT / '.cache/rag/benchmark/corpus').glob('corpus-*.parquet'))
rows = pq.read_table(source).slice(0, 250).to_pylist()
texts = [text for row in rows for parent, _, _ in splitter.split_parents(row['text'])
         for text, _, _ in splitter.pack_children(parent)][:512]
config.EMBEDDING_MODEL_SOURCE = 'LOCAL'
config.EMBEDDING_LOCAL_PATH = None
start = perf_counter()
fp32 = get_embedding_model().embed_documents(texts)
fp32_seconds = perf_counter() - start
config.EMBEDDING_LOCAL_PATH = ROOT / '.cache/rag/embedding-int8'
start = perf_counter()
int8 = get_embedding_model().embed_documents(texts)
int8_seconds = perf_counter() - start
similarity = (fp32 * int8).sum(1)
result = {'samples': len(texts), 'selection': 'First 512 packed children from first 250 official corpus records; no labels',
          'fp32_seconds': fp32_seconds, 'int8_seconds': int8_seconds,
          'speedup': fp32_seconds / int8_seconds, 'vector_cosine_min': float(similarity.min()),
          'vector_cosine_mean': float(similarity.mean()),
          'vector_cosine_p05': float(np.percentile(similarity, 5))}
(ROOT / '.cache/reports/rag/full-corpus/embedding-profile.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
