# RAG 工程交付报告

> 历史记录：2026-09-27 结构整理前的工程交付结果。路径已更新；测试数量与耗时保持原始记录，不代表本次结构整理复验。

## 交付结论与范围

四阶段 `rag_search` 已实现，并通过本轮工程回归和真实本地模型链路验证。
按最新要求，数据缩小为 300 篇公开文档、5 条固定查询；暂停全量效果评测、旧基线效果对比及阈值校准。
本报告证明链路可以运行、阶段输出可追溯，不将工程测试通过解释为准确率提升或生产性能达标。
代码尚未提交 Git；现有 `AGENTS.md` 未覆盖或修改。

## 已实现任务与技术

| 步骤 | 已实现行为 | 技术与默认设置 |
|---|---|---|
| 文档加载 | 递归读取 TXT/MD，稳定文档/段落 ID，返回原文来源和绝对偏移 | `DocLoader`、UTF-8、确定性文件排序 |
| 父子分块 | 限制块长度，合并相邻短句，避免逐句编码数量过大；保留非空白内容 | 父块 600 字符、子块 240 字符；边界与源文本一致性检查 |
| 向量编码 | 本地批量推理、归一化、查询指令；继续兼容 API embeddings | FastEmbed / ONNX Runtime；BGE；批量 32；API 响应索引校验 |
| 索引复用 | 按内容与模型指纹复用向量；文件或模型变化时失效；支持中断后续建 | SHA-256、SQLite 事务、文件锁、NumPy mmap；不再每次查询重编码全库 |
| 向量召回 | 查询与子块算余弦相似度，以子块最大分数归并父块 | NumPy 精确矩阵检索，默认取 50 个父块；不是 ANN 索引 |
| BM25 召回 | 独立检索父块；无词项匹配时返回空列表，不补任意零分结果 | jieba（HMM=False）、NFKC/casefold、rank-bm25；正 IDF；k1=1.5、b=0.75 |
| RRF 融合 | 按父块 ID 合并两路候选，去重，稳定处理同分 | 等权 `sum(1 / (60 + rank))`；不直接相加异尺度原始分数 |
| 重排序 | 对融合前 50 个候选执行真实 query/passage 联合推理 | SentenceTransformers CrossEncoder；CPU；输出原始 logit |
| 长文本重排 | 候选按 token 窗口完整处理，再取最高窗口分数 | 最大输入 512 tokens；查询最多 96 tokens；候选窗口不重叠 |
| 工具集成 | 保留注册工具的 query/strictness/breadth 接口；输出来源、正文及各阶段分数 | `rag.tool.rag_search` + Pydantic 工具参数；内部可注入 index/trace |
| 可观测与失败处理 | 记录每阶段候选、排名、分数、耗时及最终工具输出 | 调用方 trace 字典；空库提示；参数校验；模型错误上抛，不静默跳过重排 |

向量与 BM25 是独立的召回通道，执行完后融合；BM25 不局限于向量召回结果。
原有 strictness 现在作用于重排后的原始 logit，默认 strict/normal/loose 为 0/-2/-10。
本轮保留这一输出机制，但没有评估或调整这些数值，不能视为经验证的拒答边界。

## 新模块与改动文件

新增运行模块：

- `rag/index.py`：文档加载、父子索引、向量持久化和缓存失效。
- `rag/lexical.py`：中文分词、倒排候选和 BM25 打分。
- `rag/fusion.py`：RRF 融合与确定性排序。
- `rag/rerank.py`：本地 CrossEncoder 加载、窗口处理和批量重排。

改造现有 `rag/chunking.py`、`rag/embedding.py`、`rag/tool.py`、`tools/rag_search.py`；
同步 `config.py`、`.env.example`、`pyproject.toml`、`uv.lock`，将下载资源及缓存排除在 Git 外。

新增测试文件为 `dev/tests/test_hybrid_rag.py`、`dev/tests/test_rag_integration.py`、`dev/rag/archive/full_corpus/tests/test_metrics.py`（已归档）。
新增数据准备、分块审计、模型量化/测速、评测与报告脚本；其中
`dev/rag/run_engineering.py` 是本轮小规模工程验证入口。
全量 `dev/rag/archive/full_corpus/evaluate.py` / `report.py` 仅保留待后续使用，不代表已完成全量验收。

依赖增量：运行依赖显式增加 `fastembed`、`jieba`、`rank-bm25`；开发依赖增加
`onnx`（离线量化）、`pyarrow`（读取公开 Parquet 数据）。
`sentence-transformers` 原已存在，本次用于真实重排。没有引入外部搜索服务或新的向量数据库。

## 模型清单

| 模型 | 用途 | 本次变化 |
|---|---|---|
| `BAAI/bge-small-zh-v1.5` | 中文向量编码 | 延续既有模型选择，完善 FastEmbed/ONNX、本地缓存和批量推理 |
| BGE 的动态 INT8 派生权重 | 加速 CPU 工程验证 | 新生成；ONNX Runtime QInt8、per-channel、MatMul/Attention；未使用相关性标签或校准数据 |
| `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | 多语言 query/passage 重排 | 新下载并接入；权重约 471 MB；固定下载 revision `1427fd652930e4ba29e8149678df786c240d8825` |

默认应用仍使用 FP32 BGE，只有显式设置 `EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8`
才启用 INT8。本轮工程测试使用 INT8；重排模型未量化。模型权重在忽略的 `.cache/rag/` 内。
原始/量化权重 SHA-256 见 本地 `.cache/reports/rag/embedding-quantization.json`；下载来源和校验值见 本地 `.cache/reports/rag/assets.json`。
未增加生成式大模型，也没有改变 Agent 的主对话模型。

## 本轮测试数据与方法

数据来自已下载的公开 C-MTEB/T2Retrieval。对全部官方文档 ID 按 SHA-256 排序，固定取前 300 篇；
查询取此前冻结 evaluation 列表的前 5 条：18926、23071、10164、10567、3627。
选择不依赖查询文本、相关性标签或检索结果；没有向索引补入已知相关文档，也没有手工编写预期答案。
因此抽样可能缺少对应答案，不能据此计算或解释整体检索质量。

真实公共数据运行调用生产 `rag_search`，没有 mock 模型：
验证 RRF 候选等于两路并集、重排候选对应融合前 50、阶段分数按降序排列，并保存真实工具字符串。
单元测试在需要时模拟外部服务；真实模型集成测试另行验证工具注册入口、原文偏移和完整四阶段执行。

## 测试结果

- 本轮 pytest：**89 passed，3 deselected，0 skipped；18.29 秒**。排除两项 strictness 测试和旧基线适配测试；准确命令见 本地 `.cache/reports/rag/engineering/tests.json`。
- 公开数据实跑：**300 文档 → 551 父块 → 1,336 子块；5/5 查询完成**。
- 独立小规模索引首次构建：**14.795 秒**。未恢复此前的全量编码任务。
- 5 条查询耗时：**12.038、6.287、6.748、5.521、5.705 秒**；首条含重排模型初始化，后续主要耗时也在重排。
- `git diff --check` 通过。

此前两次 FP32/INT8 各 92 项通过的记录保留在 本地 `.cache/reports/rag/validation.json`，是历史验证，不能与本轮范围混为一谈。

下面列出真实阶段 Top 1，括号中为父段落起始偏移；同一文档可有多个候选段落：

| 查询 ID | 向量 Top 1 | BM25 Top 1 | RRF Top 1 | 重排 Top 1 |
|---|---|---|---|---|
| 18926 | 341982 (0) | 239273 (0) | 341982 (0) | 341982 (600) |
| 23071 | 214585 (3600) | 214585 (3600) | 214585 (3600) | 612846 (0) |
| 10164 | 578886 (0) | 161626 (0) | 161626 (0) | 578886 (3000) |
| 10567 | 618472 (0) | 332174 (0) | 281158 (0) | 340478 (0) |
| 3627 | 662860 (1200) | 172066 (0) | 662860 (1200) | 82722 (600) |

例如查询 18926：向量首位余弦 0.52592；BM25 首位 8.56266；RRF 首位 0.03252；重排首位 logit -1.93565。
这些分数不可跨阶段比较，排名变化也不代表质量改善。
所有 5 条查询各阶段 Top 3、候选数和耗时见 [历史阶段对比](rag-engineering-2026-09-27.md)；
完整候选正文、来源、分数与最终输出见 本地 `.cache/reports/rag/engineering/traces.jsonl`。

## 复现与边界

在仓库根目录、已有模型和公开数据的条件下运行：

```bash
RUN_RAG_INTEGRATION=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  .venv/bin/python -m pytest -q -k 'not strictness and not baseline_adapter'
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 RAG_THREADS=4 \
  EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 \
  .venv/bin/python dev/rag/run_engineering.py
```

本轮不执行阈值校准、Recall/MRR/nDCG、原实现质量对比、并发压测或大规模容量验收。
CPU 下 50 候选重排仍需数秒；密集向量检索仍是线性扫描；文件锁使用 POSIX `fcntl`，没有验证 Windows 支持。
查询超过 96 tokens 会截断，候选虽完整分窗但采用最高窗口分数。
这些是现有工程边界；后续可根据实际延迟目标和业务语料再决定优化，不阻塞本轮交付。
