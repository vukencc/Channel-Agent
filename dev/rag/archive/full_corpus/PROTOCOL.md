# RAG evaluation protocol

Status: the full-corpus protocol below is deferred at the user's request.
Current delivery uses a separate engineering run: 300 corpus documents selected
by SHA-256 ID order and the first 5 already-frozen evaluation queries. No qrels,
threshold tuning, or quality metrics are used. Its manifest, real stage traces
and results are in local `.cache/reports/rag/engineering/`; the original manifests below remain intact.

Frozen before retrieval: `inputs/queries.json` sorts all official query IDs by the hex
SHA-256 of their UTF-8 ID. Positions 0–49 calibrate the output gate; positions
50–149 evaluate it. No filtering by labels, query wording, or retrieval outcomes.
The complete official T2Retrieval corpus is retained. This is a fixed-query
subset evaluation, not an official full C-MTEB score.

Data: https://huggingface.co/datasets/C-MTEB/T2Retrieval and
https://huggingface.co/datasets/C-MTEB/T2Retrieval-qrels. local `.cache/reports/rag/assets.json` records
repository revisions and downloaded-file SHA-256 checksums. The pre-change
source snapshot and checksums are in `baseline/`.

All stages run through `rag.tool.rag_search`, with the public corpus adapted to
the same document input type. The filesystem loader and registered tool are
additionally tested with the unchanged repository corpus. No relevance labels
are provided to the index, retrievers, fusion or reranker. Labels are used only
by the scorer and output-threshold calibration after retrieval.

Ranking parameters are fixed: 50 parents per channel, equal-weight RRF with
k=60, rerank the first 50 fused parents. Dense search takes each parent's maximum
child cosine similarity. Adjacent sentences are packed up to 240 characters;
this bounds compute and preserves contextual spans without omitting text. BM25 ranks parent texts independently. Reranking scores
all token windows of each candidate parent and takes the maximum raw logit.
All source documents are indexed, including long texts; bounds split rather
than remove text. Missing lexical overlap produces no BM25 candidates.

Report raw vector, BM25, RRF and rerank stages before output filtering. For
Recall@10, MRR@10 and nDCG@10, collapse parent hits to unique original document
IDs in ranking order, then take the first 10. Use official positive qrels;
unjudged documents count as nonrelevant. This annotation assumption is a
limitation, not evidence that unjudged content is actually irrelevant.
The `filtered` row separately measures the actual top-10 parent output gate.

The baseline uses frozen original splitting, unprefixed query embeddings,
cosine scoring, threshold 0.45 and parent deduplication. Cache document vectors
and construct once for tractable full-corpus evaluation; do not compare its
warm query latency to the original repeated construction cost. Retrieve up to
50 parents for the same unique-document @10 scoring as other stages. The new
vector stage also changes chunk limits and adds the BGE query instruction;
the baseline-to-final delta therefore includes those changes.

Calibrate strict/normal/loose thresholds using F0.5/F1/F2 on calibration top-10
parent logits only. Enforce strictly descending thresholds with a minimum 0.1
separation. Freeze the result before running the evaluation queries. Raw logits
are model-specific scores, not calibrated probabilities or proof of answerability.

Keep every query's ranks, scores, source text, latency and final tool output in
compressed JSONL. Publish aggregate metrics and all regressions, not only wins.
Never replace real integration/quality runs with mocked model scores. Unit-test
fixtures verify arithmetic/contracts only and are excluded from quality claims.

BM25 uses rank_bm25 TF saturation (k1=1.5, b=0.75) with the positive
`log(1 + (N - df + 0.5)/(df + 0.5))` IDF documented by
[Apache Lucene](https://lucene.apache.org/core/9_9_1/core/org/apache/lucene/search/similarities/BM25Similarity.html).
This avoids negative common-word weights on the repository's small corpus.
No corpus-specific stopword list or query-specific dictionary is introduced.

CPU execution uses the same dynamically quantized INT8 BGE encoder for both
algorithms. This is an **original retrieval-logic baseline with a common INT8
encoder**, not a bit-for-bit replay of the original FP32 model. The FP32 real
integration suite was run separately. `embedding-quantization.json` records
source/output weight hashes, and `embedding-profile.json` reports the fixed
512-span vector-agreement and throughput check performed before quality runs.
No evaluation labels were used for quantization or runtime selection.
