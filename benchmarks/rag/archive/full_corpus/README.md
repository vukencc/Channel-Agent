# 暂停的全量 RAG 评测

状态：暂停，未完成质量验收。此目录仅用于后续显式恢复，不被小规模工程入口或默认 `tests/` 导入。

- `inputs/queries.json`：冻结的 50 条校准、100 条评测查询和原始输入校验值。
- `baseline/`：改造前源码及 SHA-256 清单；归档移动不改变其内容。
- `PROTOCOL.md`：原始评测方法，保留历史背景。
- `evaluate.py`、`report.py`：完整语料评测与报告生成。
- `audit_chunks.py`、`profile_embedding.py`：全库分块审计、编码器对比。
- `tests/`：指标计算和基线适配测试，仅显式运行。

仅在需要恢复时运行，CPU 全量编码可能耗时数小时：

```bash
uv run python scripts/rag/prepare_assets.py
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=8 RAG_THREADS=8 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  uv run python benchmarks/rag/archive/full_corpus/evaluate.py --phase all
uv run python benchmarks/rag/archive/full_corpus/report.py
uv run pytest benchmarks/rag/archive/full_corpus/tests/test_metrics.py
```

`evaluate.py` 强制要求显式 `--phase`，不会因为省略参数就启动全量运行。
`evaluation` 阶段需要先生成校准文件；输出统一在忽略的 `reports/rag/full-corpus/`。
恢复前确认本地模型、qrels 和 corpus 已准备好。原中断编码缓存保留于 `.cache/rag/`。
结构整理前的本地历史输出仍在 `reports/rag/`，不会自动作为新运行输入。
