# 暂停的全量 RAG 评测

状态：暂停，未完成质量验收。此目录仅用于后续显式恢复，不被小规模工程入口或默认 `dev/tests/` 导入。

- `inputs/queries.json`：冻结的 50 条校准、100 条评测查询和原始输入校验值。
- `baseline/`：改造前源码及 SHA-256 清单；归档移动不改变其内容。
- `PROTOCOL.md`：原始评测方法，保留历史背景。
- `evaluate.py`、`report.py`：完整语料评测与报告生成。
- `audit_chunks.py`、`profile_embedding.py`：全库分块审计、编码器对比。
- `tests/`：指标计算和基线适配测试，仅显式运行。

仅在需要恢复时运行，CPU 全量编码可能耗时数小时：

```bash
uv run python dev/rag/prepare_assets.py
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=8 RAG_THREADS=8 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  uv run python dev/rag/archive/full_corpus/evaluate.py --phase all
uv run python dev/rag/archive/full_corpus/report.py
uv run pytest dev/rag/archive/full_corpus/tests/test_metrics.py
```

`evaluate.py` 强制要求显式 `--phase`，不会因为省略参数就启动全量运行。
`evaluation` 阶段需要先生成校准文件；输出统一在忽略的 `.cache/reports/rag/full-corpus/`。
恢复前确认本地模型、qrels 和 corpus 已准备好。原中断编码缓存保留于 `.cache/rag/`。
结构整理前的本地历史输出仍在 `.cache/reports/rag/`，不会自动作为新运行输入。

## 旧基线专用依赖

当前应用和全量评测适配器不使用 ChromaDB。冻结的旧 `baseline/tool.py` 仍包含该历史 import；
源码及校验值原样保留，仅显式重放旧基线测试时安装它：

```bash
RUN_RAG_INTEGRATION=1 uv run --with 'chromadb==1.5.9' pytest dev/rag/archive/full_corpus/tests/test_baseline.py
```

这不会把旧基线依赖加回当前应用的 `pyproject.toml` 或 `uv.lock`。scikit-learn 已由实际重排依赖引入。
