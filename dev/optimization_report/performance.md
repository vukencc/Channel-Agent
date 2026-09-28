# 性能瓶颈（PERF）

> 所有 `file:line` 为基线 `7e7499b`。没有前后测量数据的优化不算完成。

---

## PERF-01 重排固定跑 50 个候选、CPU 硬编码、无缓存

**现状**
- `rag/tool.py:55`：每次检索对 `max(RAG_RERANK_TOP_N, limit)` 个父段落重排，默认 `RAG_RERANK_TOP_N=50`，与 breadth 无关。
- `rag/rerank.py:20`：`device='cpu'` 硬编码；`max_length=512`（:20）、查询截断 96 token（:32）、长段落按 512 窗口切分且逐窗打分（:39）、`RAG_BATCH_SIZE=32`。
- 无重排结果缓存；同一 query 重复检索完整重跑。
- 工程实测（`docs/rag-engineering-2026-09-27.md`）：50 候选重排 5.5~12 秒。

**优化方向**
1. `RAG_RERANK_DEVICE`（cpu/cuda/mps，自动探测）、`RAG_RERANK_DTYPE`（fp32/fp16/int8）或 ONNX 后端。
2. 候选预算按 breadth 自适应（narrow 20 / normal 30 / wide 50），并支持 `RAG_RERANK_TOP_N` 覆盖；先过滤 `-inf` 再重排。
3. 重排结果 LRU 缓存：键 `(query, parent_ids, model, window)`；query 向量同样缓存。
4. 长段落改为「代表窗口 + 尾部窗口」而非全窗口平铺，减少 pair 数。
5. 支持传入 `top_k`（当前工具层未暴露，见 FREE-07）。

**验收**：固定语料上检索 p50 延迟下降 ≥40% 且 `dev.rag.quality` 指标不退化；新增 `RAG_RERANK_DEVICE` 文档与测试。

---

## PERF-02 向量召回为全量暴力点积，无 ANN

**现状**：`rag/index.py:194-205` 每次查询把 `self.vectors`（全部子块）与 query 做矩阵乘法；无 HNSW/IVF/PQ；大语料线性变慢、内存驻留全部向量。

**优化方向**
1. 可选 ANN 后端（hnswlib/faiss 作为 optional extra），`RAG_VECTOR_BACKEND=exact|ann`，默认 exact 保持行为。
2. ANN 阈值自动切换（子块数 > N 时），构建结果持久化到 `RAG_CACHE_DIR`。
3. 保留 exact 回退与 recall 对比脚本。

**验收**：10 万子块基准下 dense 召回 <200ms；输出 recall@50 对比报告；默认 exact 行为与现有一致。

---

## PERF-03 BM25 变更全量重建、不持久化

**现状**：`rag/index.py:192` 任一文档变化都会新建 `RetrievalIndex` → 对全部父块重建 `BM25Index`；`rag/lexical.py:24-34` 重建 postings/idf；进程重启也全量重建。分词 `lru_cache` 只缓解 tokenize 重复。

**优化方向**
1. postings 按文档增量增删，IDF 增量更新。
2. BM25 结构与语料指纹持久化（如 `RAG_CACHE_DIR/bm25-*.pkl`），启动直接加载。
3. 变化时后台重建 + 双缓冲切换，检索不阻塞在重建锁上。

**验收**：单文件变更的重建耗时与语料总量基本无关；重启后首次检索无需重建；现有 `test_hybrid_rag.py` 全绿。

---

## PERF-04 语料与分块常驻内存

**现状**：`rag/index.py:24` `_document_cache` 保存全文（最多 4 个根）；`:106` `split_document` `lru_cache(4096)` 以全文为键、持有分块元组；叠加 parents/children、BM25 postings、memmap 向量，大语料内存成倍增长。

**优化方向**
1. 正文改为磁盘缓存（内容哈希文件名）或按需读取，内存只保留索引与元数据。
2. 分块缓存按字节上限 LRU（`RAG_MEMORY_LIMIT_MB`）。
3. 提供 `dev/perf/benchmark.py --rss` 的内存基线。

**验收**：10 万文档下 RSS 峰值可配置上限内；检索结果与当前一致。

---

## PERF-05 启动加载全部会话与消息

**现状**：`core/sessions.py:54` → `core/storage.py:186-210` `load_all()` 读取每个 `session.json` 并加载完整 `messages.jsonl`；会话多时启动慢、内存占用与历史总量线性相关。

**优化方向**
1. 元数据索引（标题/状态/updated_at/turn 数）与消息内容分离，启动只读索引。
2. `/switch` 或提交时才懒加载目标会话消息，支持分页读取。
3. `--list` 只读元数据。

**验收**：100 个 10MB 会话启动 <1s；内存不随消息总量线性增长；恢复、迁移与现有测试不回归。

---

## PERF-06 上下文构建重复序列化与深拷贝

**现状**：`core/context.py:33-151`：`build_model_history` 先 `deepcopy` 全量、再对每条消息 `json.dumps` 一次；`request_tokens(messages, schemas)`（:37）与逐条序列化重复；命中压缩时 `prepare_model_history`（:121-151）会二次构建并 `json.dumps` 被移除轮次。大历史每轮都在重复这些工作。

**优化方向**
1. 每条消息的序列化/估算只做一次并缓存到调用内计算结构。
2. 用浅拷贝 + 不可变消息约定替代 `deepcopy`（消息追加后不修改）。
3. 可选 `orjson` 加速（fallback 标准库）。
4. 摘要结果按内容哈希缓存到磁盘（跨重启复用）。

**验收**：1 万消息会话的单轮上下文准备耗时下降 ≥50%；`test_context.py`、`test_bug08_summary.py` 全绿。

---

## PERF-07 命令配额检查每 0.1s 全量扫描工作区

**现状**：`tools/command.py:83` 在轮询循环内调用 `check_workspace_quota()`；`tools/sandbox.py:239-255` 用 `os.walk` 遍历整个工作区。大工作区下命令期间 CPU 与 IO 开销明显。

**优化方向**
1. 节流（例如每 1s 或仅在开始/结束 + 采样）。
2. 维护增量用量统计（写工具上报、命令结束重算一次）。
3. 消息中说明配额为「采样 + 结束校验」，并用 rlimit FSIZE 兜底单文件。

**验收**：大工作区（数千文件）命令期间 CPU 占用显著下降，超额仍被拒绝且审计不变。

---

## PERF-08 审计每条事件打开/关闭文件

**现状**：`tools/sandbox.py:117-135` 每个审计事件 `open(..., 'a')` + 写入；高频工具调用（read_file/list_files）产生大量打开/关闭。

**优化方向**：进程内持久追加句柄或 `logging.FileHandler`；提供 `audit_sync` 选项控制 fsync；保证崩溃不丢已确认事件。

**验收**：1 万次审计写入微基准耗时下降 ≥50%；审计内容与现有一致。

---

## PERF-09 本地推理与线程池竞争

**现状**：检索/重排在 `asyncio.to_thread`（`core/sessions.py`）执行 torch，`TOOL_CONCURRENCY=4` 允许多个会话同时重排；`torch.set_num_threads(RAG_THREADS)` 全局；`rag/index.py:113-122` 构建期 flock 全局串行。

**优化方向**
1. RAG 专用并发限制（默认 1~2），与文件工具池分离。
2. 可选 `ProcessPoolExecutor` / 独立 worker 进程做重排与嵌入，避免 GIL 与主进程内存竞争。
3. 交互检索优先于后台索引构建（优先级队列）。

**验收**：4 并发会话检索无超线性劣化；单会话检索 p95 不高于当前。

---

## PERF-10 模型请求无前缀缓存、schema 全量重复发送

**现状**：`core/llm.py:154-157` 每轮发送完整 `history` + `TOOL_SCHEMAS`；系统提示每轮追加 guide/记忆/预算（`core/sessions.py:285-290`）；未使用任何 provider 前缀缓存能力。

**优化方向**
1. 稳定前缀排序：固定的 schema 与基础系统提示在前，动态内容（记忆/预算）放尾部，减少前缀失效。
2. 按 provider 支持启用 prompt caching（如显式 cache 断点）。
3. 会话级工具子集（FREE-05 配套），默认全量保持现状。
4. schema 描述精简，压缩每轮固定开销。

**验收**：使用 provider usage 统计，同一会话第二轮起 input tokens 明显下降；功能测试不变。

---

## PERF-11 摘要/评估/记忆提取与对话争模型

**现状**：摘要同步发生在轮内（`core/context.py:133-135`，`SUMMARY_TIMEOUT=10`）；评估与记忆提取共用 `assessment_slots`（`ASSESS_CONCURRENCY=1`）且固定主模型；三者都会拖慢或排队在主对话之后。

**优化方向**
1. 摘要可延后到回答完成后后台执行，或预生成下一轮摘要。
2. 后台任务使用可配置的廉价 `AUX_MODEL`（默认主模型）。
3. 独立超时与失败降级（已有），增加优先级与并发配置。

**验收**：开启摘要时单轮交互延迟增量小于可配阈值；后台任务不与主对话互锁。

---

## PERF-12 导出/归档全内存构建

**现状**：`core/storage.py:261-277` 导出时拼接完整 Markdown/JSON 文本再原子写入；大会话内存峰值与耗时高。

**优化方向**：流式写临时文件（逐消息 write + fsync + rename）；可选 gzip；导出进度提示。

**验收**：100MB 会话导出内存峰值 <32MB，内容与现有导出一致。

---

## PERF-13 CLI 历史渲染硬编码与全量 join

**现状**：`core/transcript.py:6-7` `PAGE_LINES=180`、`LINE_CHARS=160` 硬编码；每页 `'\n'.join`；超长会话滚动依赖手动分页。

**优化方向**：页大小/行宽配置化；分页结果缓存失效策略优化；超长列表虚拟滚动。

**验收**：10 万条消息会话翻页与跟随最新无明显卡顿；显示内容不丢失。

---

## PERF-14 每步保存 fsync

**现状**：`core/storage.py:102-134` 每次保存都 JSONL append + `fsync` + `session.json` 原子重写；崩溃安全但每轮 IO 次数多。

**优化方向**：可配 `SAVE_FSYNC=always|interval|off`（默认 always 保持现状）；批量去抖；文档说明不同等级的丢失窗口。

**验收**：默认语义不变；非默认档位有崩溃恢复测试与文档说明。

## 实施追加记录

### PERF-05（完成）

100 × 10 MiB v2 会话，管理器加载 1.969569 → 0.003993 秒，峰值 RSS 增量 1016.246 → 0 MiB。
方法与限制见 `docs/optimization-progress.md`；基准入口 `dev/perf/benchmark.py`。
历史校验与中断恢复延后到目标访问，列表只读，保留 v1 兼容。

### PERF-06（完成）

10,001 条消息，3 次中位 0.732597 → 0.053915 秒，减少 92.64%；输出计量一致。详见 docs/optimization-progress.md。

### PERF-07（完成）

真实沙箱 3,000 文件基准：104 → 7 次扫描；墙钟 1.776154 → 0.582655s，CPU 1.775906 → 0.132174s。26 项回归通过。默认仍 0.1s 采样，保留前后检查。

### PERF-08（实现已验证，性能验收未达）

10,000 事件 0.128443 → 0.081054 秒（下降 36.90%，低于 50% 目标）。持久句柄、轮转、并发、fsync 回归通过；保留立即轮转识别。详见 docs/optimization-progress.md。

### PERF-12（完成）

100 MiB 导出 0.938152 → 0.661944s，Python 峰值分配 400.817 → 0.356 MiB。JSON 格式等价，损坏不发布。完整离线回归 203 passed, 3 deselected。

### PERF-08（补充验收完成）

有界公共字段缓存后，5×10,000 事件逐条校验：历史 3ae9a2a 真代码中位 0.156593s，当前 0.047309s，降低 69.79%。保留前次未达标数据，完整复测方法见 docs/optimization-progress.md。49 项审计/文件/命令回归通过。

### PERF-01（完成，缓存热查询收益）

固定公开 343 文档/10 查询两轮：首次 p50 5.998842 → 6.779045s，重复 p50 5.954295 → 0.001340s。开启有界结果缓存；默认不变。四阶段结果完全一致，质量无退化，GPU 未验证。详见 docs/optimization-progress.md 与 docs/perf-rag-comparison.md。
