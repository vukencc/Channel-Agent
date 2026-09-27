# Hybrid RAG

`rag_search` executes independent dense and BM25 retrieval, reciprocal rank
fusion (RRF), then a real local cross-encoder. It returns parent passages with
source offsets and separately named scores. `strictness` is now a final raw
reranker-logit gate, not a cosine gate; `breadth` still maps to 2/4/8 results.
The direct Python API additionally accepts `top_k`, an injected `RetrievalIndex`,
and a caller-owned `trace` dictionary. The registered agent tool stays unchanged.

## Development

Current delivery scope is engineering validation. The full-corpus benchmark and
threshold calibration are deferred. Run the small, label-independent public
corpus check (300 documents, 5 frozen queries) with existing downloaded assets:

```bash
RUN_RAG_INTEGRATION=1 OPENBLAS_NUM_THREADS=1 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  uv run pytest -q -k 'not strictness and not baseline_adapter'
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 RAG_THREADS=4 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  uv run python scripts/test_rag_engineering.py
```

Results live in `reports/rag/engineering/`: fixed input IDs, complete real stage
traces, timings and `STAGES.md`. Sampling uses SHA-256 document IDs independently
of query text and relevance labels. Relevant documents may be absent; this check
establishes execution and trace integrity, not retrieval accuracy. The ordinary
production output gate remains in place, but its thresholds are not evaluated
or tuned. See `reports/rag/DELIVERY.md` for the delivered implementation.

The following full evaluation workflow is retained for future use; it is **not**
required for current engineering acceptance and can take hours on CPU:

```bash
uv sync --locked
uv run pytest -m 'not integration'
uv run python scripts/prepare_rag_benchmark.py
RUN_RAG_INTEGRATION=1 uv run pytest -m integration
uv run python scripts/quantize_rag_embedding.py
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=8 RAG_THREADS=8 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  uv run python scripts/evaluate_rag.py --phase all
uv run python scripts/report_rag.py
```

Preparation downloads pinned public data and only the required reranker format;
`--endpoint https://hf-mirror.com` is available when the official host is
unreachable. Existing segment checkpoints support interrupted downloads. Assets
live in ignored `.cache/rag/`; small manifests and reports live in `reports/rag/`.
Downloaded weights are never committed. Real integration tests require real
models and fail on load/inference errors when enabled; skipped tests do not
constitute model validation.

The benchmark uses the complete official T2Retrieval corpus with 50 calibration
and 100 evaluation queries selected before retrieval by SHA-256(query ID).
`reports/rag/PROTOCOL.md` defines the scoring, baseline and selection rules.
`--phase index`, `baseline`, `calibration`, and `evaluation` allow separate runs;
evaluation requires the previously frozen calibration file. Raw stage metrics
remain independent of output filtering. Review all regressions in `per-query.csv`.

## Configuration and cache

See `.env.example` for candidate budgets, chunk bounds, batch/thread controls,
model paths and thresholds. `EMBEDDING_LOCAL_PATH` accepts an existing FastEmbed
ONNX directory. Otherwise the embedding model uses `.cache/rag/models`.
`RERANK_LOCAL_PATH` accepts an existing CrossEncoder directory. The preparation
script's `.cache/rag/reranker` is automatically reused when its recorded model
and revision match configuration. API embeddings remain supported; the local
BGE query instruction is never added to arbitrary API models.

Defaults: 50 candidates per channel; RRF k=60; rerank 50 candidates; 600-character
parents and 240-character children formed by packing adjacent sentences. Dense search scores children then collapses
to each parent's maximum. BM25 uses the entire parent text and jieba with HMM
turned off. No query-specific dictionaries, special-case answers or relevance
labels are used by the pipeline. Reranking windows long candidate passages;
it reserves at most 96 query tokens and uses the maximum passage-window logit.

Content/model hashes address persistent embedding vectors; corpus/configuration
changes rebuild the in-memory index. The corpus is read to detect same-size
edits as well as additions/removals. SQLite transactions checkpoint vectors,
and complete matrices are memory-mapped. Importing modules does not load models.
Empty corpora return an explicit status; missing directories and invalid inputs
raise errors. Reranker failure is propagated, never disguised as successful RRF.

Scores from cosine, BM25, RRF and cross-encoder logits have different scales.
Reranker logits are not probabilities or reliable out-of-domain rejection
scores. Calibrate thresholds on held-out development labels for your own corpus;
do not infer universal answerability from the public benchmark thresholds.

The recorded full-corpus run uses the same INT8 BGE encoder in the original
retrieval-logic baseline and the hybrid pipeline. It is not a bit-for-bit FP32
replay. Weight checksums and a fixed 512-span FP32/INT8 comparison are included
in the report artifacts. Quantization uses no calibration data or labels.
Default application embeddings remain FP32 unless a local INT8 path is selected.
