# Hybrid RAG

`rag_search` 执行独立向量召回和 BM25 召回，再经 RRF 融合、真实本地 CrossEncoder 重排。
返回父段落正文、来源偏移和各阶段分数。注册工具参数不变；Python 接口额外支持 `top_k`、`index`、`trace`。

## 工程测试

当前范围是功能及真实链路验证，不评价或校准搜索阈值。安装环境使用 `uv sync --locked`。

```bash
uv run pytest -m 'not integration'
RUN_RAG_INTEGRATION=1 OPENBLAS_NUM_THREADS=1 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  uv run pytest -q -k 'not strictness'
```

集成测试显式启用后使用真实本地模型，模型加载或推理失败直接报错。
单元测试的模拟数据仅验证工程行为，不作为质量评估。

## 小规模公开数据验证

`dev/rag/inputs/engineering.json` 固定了公开 T2Retrieval 的 300 篇文档 ID、5 条查询、源文件和子集校验值。
文档按 ID 的 SHA-256 排序选择，不依据查询、相关性标签或检索结果挑选。

首次准备需要下载公开语料及模型（已有资源会复用）：

```bash
uv run python dev/rag/prepare_assets.py --only corpus
uv run python dev/rag/prepare_assets.py --only embedding
uv run python dev/rag/prepare_assets.py --only reranker
uv run python -m dev.rag.prepare_engineering
# 可选 INT8 加速：输入应为原始 FP32 模型。
uv run python dev/rag/quantize_embedding.py
```

准备程序流式读取完整 Parquet、验证 SHA-256，只提取固定文档到
`.cache/rag/benchmark/engineering/documents.jsonl`。之后运行只需要这个子集、输入清单与模型，
不再加载完整语料、不读取 qrels、不导入全量评测脚本。

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 RAG_THREADS=4 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  uv run python -m dev.rag.run_engineering
```

`prepare_engineering` 支持 `--source/--manifest/--output`；`run_engineering` 支持
`--corpus/--manifest/--output`，可指定单独的本地输入与输出位置。
默认输出到忽略的 `.cache/reports/rag/engineering/`：

- `manifest.json`：本轮输入 ID、查询及运行参数快照，不作为下次运行的输入。
- `traces.jsonl`：四阶段完整候选、正文、分数、耗时及工具返回值。
- `summary.json`、`STAGES.md`：工程执行摘要与各阶段 Top 3 对比。

相关文档可能不在固定子集中；运行通过不能证明检索准确率提升。
输入清单保存在版本控制中，下载数据和输出都不提交。人工审阅的历史记录独立保存在
[交付报告](rag-delivery.md)和[阶段结果快照](rag-engineering-2026-09-27.md)。

## 运行配置

`.env.example` 定义模型、批量/线程、候选数和分块设置；密钥留在忽略的 `.env`。
默认每路召回 50 个父块，RRF k=60，重排前 50 个；父块 600 字符、子块 240 字符。
向量取各父块的最大子块分数；BM25 使用 jieba（HMM=False）和正 IDF。
重排最大输入 512 tokens，查询最多 96 tokens，长候选完整分窗后取最大 logit。

`EMBEDDING_LOCAL_PATH` 指定 FastEmbed ONNX 目录；未指定 INT8 时默认仍是 FP32 BGE。
`RERANK_LOCAL_PATH` 指定 CrossEncoder 目录；下载脚本生成的 `.cache/rag/reranker` 可自动复用。
API embeddings 继续支持，不附加本地 BGE 专用查询指令。
内容/模型指纹控制 SQLite 向量缓存和 mmap 矩阵复用；目录、文本、模型变化会失效重建。
当前为 NumPy 精确向量检索，文件锁依赖 POSIX，未验证 Windows。

`strictness` 控制重排后的 logit 门槛，`breadth` 对应 2/4/8 条。
余弦、BM25、RRF 与 logit 尺度不同；logit 不是概率，默认阈值尚未校准。
空库返回状态说明，参数错误或模型故障上抛，不静默退化成跳过重排。

## 暂停的全量评测

全量 118,605 文档、50 校准查询、100 评测查询的流程归档至
[archive/full_corpus](../dev/rag/archive/full_corpus/README.md)。
它不属于默认 pytest，不参与工程测试，只有显式执行归档命令才运行。
原始基线源码和固定查询完整保留；全量产物仍仅留本地，不代表已完成质量验收。

## 文件增量更新与加载器

文件缓存使用 size/mtime_ns/ctime_ns/inode 指纹，只有变化的文件重新读取并哈希；分块与分词使用有界缓存。
未变的索引直接返回，不等待重建锁；变化请求构建完整新索引后原子替换，避免返回旧语料。
最小版本仍需重算全局 BM25 IDF 和组合向量矩阵，首次或变化请求同步等待；不提供后台陈旧索引。
原生支持 txt/md/html/htm（HTML 忽略 script/style）。PDF/Word 可运行 `uv sync --locked --extra documents` 安装可选解析器；
缺失依赖时警告并跳过，解析错误仍明确失败。无 OCR、无网页抓取，原始资料不改写。

补充：Linux inotify 文件事件用于捕获同尺寸、同时间戳的快速改写。事件能力缺失或队列溢出时保守重读，避免陈旧索引；统计指纹不是唯一失效依据。
