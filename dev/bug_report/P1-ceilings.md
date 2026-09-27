# P1 能力上限问题（P0 之后处理）

> 基线提交：`93a7fed`。严重级别与工作流见 [README.md](README.md)。
> 这些条目不一定立刻失败，但决定项目能否称为「标准充足」的 Agent。

---

## BUG-08 无 token 计量、无摘要式上下文压缩

**级别** P1　**状态** 已修复（说明见下）　**文件** `core/context.py`、`config.py`

**症状**
- 预算是字符数而非 token 数，CJK 与 ASCII 混排时误差大。
- 超限时只能省略或整轮丢弃旧消息，信息不可恢复，且更容易触发 BUG-01。

**证据**
- `core/context.py:42-64`：压缩策略为省略旧读取页、长写入参数、整轮淘汰，无摘要。
- `config.py:69-70`：`MODEL_INPUT_CHARS` / `MODEL_OUTPUT_CHARS` 为字符数。

**修复建议**
1. 近似 token 估算（CJK 字符约 1 token、ASCII 约 4 字符/token），新增 `MODEL_INPUT_TOKENS`（保留字符参数作兼容），预算含工具 schema、system、记忆。
2. 淘汰旧轮次前，用 `complete()` 生成「历史摘要」替换整轮内容；摘要失败降级为现有省略逻辑。
3. 摘要内容带明确标记，不参与工具配对校验，不写入完整记录（仅工作副本）。
4. 测试用 fake judge/complete，不依赖真实模型。

**验收**
- 长会话可持续压缩且保留关键事实；`last_run.context` 记录压缩前后 token 估算。
- 不新增硬依赖（tiktoken 等作为可选）。

---

**修复说明** 字符预算不能反映混合文本成本。core/context.py 增加 ASCII/非 ASCII token 估算、双预算与异步摘要；摘要限制输入输出和时限，仅修改工作副本，失败降级，按内容摘要缓存。core/sessions.py 接入并记录前后估算。新增 test_bug08_summary.py，fake judge 验证事实保留。新配置 MODEL_INPUT_TOKENS=16000、CONTEXT_SUMMARY=True、SUMMARY_INPUT_CHARS=12000、SUMMARY_CHARS=1000、SUMMARY_TIMEOUT=10。估算不是精确 tokenizer。 验证：`uv run pytest dev/tests/test_bug08_summary.py dev/tests/test_context.py dev/tests/test_sessions.py -q（22 passed）`。

## BUG-09 长输出全有或全无，截断后只能一次性恢复

**级别** P1　**状态** 已修复（说明见下）　**文件** `core/llm.py`、`core/sessions.py`、`config.py`

**症状**
- 单次输出上限 48000 字符，超限或不完整结束即整轮失败。
- 大文件生成只能靠提示词自律拆小步；达到 24 轮后进入 checkpoint 等待人工继续。

**证据**
- `core/llm.py:176-177`：超 `MODEL_OUTPUT_CHARS` 抛 `ModelResponseError`，不执行工具。
- `core/llm.py:187-188`：`finish_reason` 非 stop/tool_calls 同样失败。
- `core/sessions.py:236-256`：只允许一次「小步骤恢复」；`:288-294` 预算用尽转 checkpoint。
- 项目自述实验见 `docs/large-file-recovery.md`：多轮真实重放均未自动完成全部细化要求。

**修复建议**
1. 识别「纯文本截断」与「工具参数截断」，前者允许一次显式续写请求（携带已生成前缀），后者保持禁止执行。
2. 或提供 `append_file` 工具与「分段生成」协议，明确要求每段落盘；两种方案择一，避免协议复杂化。
3. continuation 请求不得重放已成功工具，续写结果需校验完整性。
4. 测试：fake 流在 `length` 处结束，验证续写与不重复副作用。

**验收**
- 60k 字符级输出任务可分段落盘，无需人工「继续」或最多一次人工介入。
- 不完整工具调用仍永不执行。

---

**修复说明** 大文件单次参数易截断。采用分段生成协议：tools/file_crud.py 增加 append_file（4000 字符、预期偏移、逐次确认、原子写与审计）；core/prompts.py 指导逐段落盘，core/context.py 压缩历史分段参数。测试实际保存 60k 中文字符并拒绝重复偏移、拒绝确认；原有不完整流不执行测试保持。无新增配置。 验证：`uv run pytest dev/tests/test_bug09_append.py dev/tests/test_file_crud.py dev/tests/test_llm_stream.py -q（36 passed）`。

## BUG-10 保存放大：事件循环 deepcopy + 每步全量重写

**级别** P1　**状态** 已修复（说明见下）　**文件** `core/storage.py`、`core/cli.py`

**症状**
- 每步保存都在事件循环上 `deepcopy` 整份会话，长会话造成 UI 卡顿。
- 每步全量重写并 fsync `session.json`，IO 与磁盘放大随会话长度线性增长。
- `/export` 在 UI 线程复制整份记录。

**证据**
- `core/storage.py:92-96`：`save_async` 在事件循环执行 `copy.deepcopy(record)`。
- `core/storage.py:98-104`：`save` 每次 `json.dumps` 全文 + 原子替换。
- `core/cli.py:334`：导出前 `copy.deepcopy(session.record)`。

**修复建议**
1. 消息改为增量 JSONL 追加（append + fsync），`session.json` 作为周期性快照/索引；恢复时合并。
2. 或至少：保存去抖 + 只序列化新增消息；快照一致性用不可变消息约定保证。
3. 导出快照移入写线程（保持一致性前提下）。
4. 增加会话体积上限与归档/清理策略。
5. 兼容旧格式：通过 `version` 字段做一次无损迁移，保留旧文件备份。

**验收**
- 10k 消息会话的保存耗时与写入量不随步数线性恶化。
- 崩溃恢复与现有 `test_sessions.py`、慢磁盘测试全过。

---

**修复说明** 保存时全量 deepcopy/序列化造成线性放大。core/storage.py 改为版本 2 消息 JSONL 增量追加及小型原子提交索引，旧格式备份后迁移，崩溃尾部不重放；异步保存只复制元数据并捕获不可变消息边界。core/cli.py 导出在线程读取已提交快照。SESSION_MAX_MB=64 控制新轮次准入，归档保留全部内容。test_bug10_storage.py 使用 10k 消息验证索引小于 5 KB、新消息写入小于 100 B及旧版备份/崩溃尾部恢复。 验证：`uv run pytest dev/tests/test_bug10_storage.py dev/tests/test_sessions.py dev/tests/test_cli.py dev/tests/test_cli_performance.py -q（26 passed）`。

## BUG-11 工具串行执行、单轮调用无上限、无工具级超时

**级别** P1　**状态** 已修复（说明见下）　**文件** `core/sessions.py`、`tools/base.py`

**症状**
- 模型返回的并行 `tool_calls` 被逐个同步执行，多文件/多检索任务耗时线性叠加。
- 单轮工具数量没有上限，异常模型可触发大量调用。
- 工具无统一超时元数据（命令有 `COMMAND_TIMEOUT`，检索/网络没有）。

**证据**
- `core/sessions.py:268-287`：`for call in calls` 顺序执行。
- `tools/base.py:9-24`：`Tool` 无超时/并发属性。
- `tools/command.py:67` 由工具内部自行超时。

**修复建议**
1. 为只读/无冲突工具引入 `asyncio.gather` 并发执行，结果按原顺序回填；写文件的工具保守串行（或按路径加锁）。
2. 新增 `MAX_TOOL_CALLS_PER_ROUND`。
3. 在 `Tool` 上增加可选 `timeout_s` 与 `concurrency` 元数据，统一由循环执行。
4. 测试：慢工具并发耗时对比；取消时不再重复副作用。

**验收**
- 多检索/多读取任务耗时接近单次最长调用而非总和。
- 取消、确认、审计语义不变。

---

**修复说明** 工具循环串行且无调用总数边界。tools/base.py 增加 concurrency/timeout_s 元数据；core/sessions.py 有界并发连续只读批次、按原顺序回填结果、写操作串行、超额调用明确拒绝并配对，取消等待旧写线程。新增 MAX_TOOL_CALLS_PER_ROUND=8、TOOL_CONCURRENCY=4、TOOL_TIMEOUT=120。test_bug11_tools.py 验证慢读取并行和超额不执行；线程级取消边界见 CLI 文档。 验证：`uv run pytest dev/tests/test_bug11_tools.py dev/tests/test_sessions.py dev/tests/test_cli_performance.py -q（22 passed）`。

## BUG-12 RAG 每次搜索全量重读语料、仅支持 txt/md

**级别** P1　**状态** 已修复（说明见下）　**文件** `rag/index.py`

**症状**
- 每次 `rag_search` 都重读并哈希全部文档，大语料下搜索延迟与磁盘 IO 高。
- 语料只支持 `.txt`/`.md`，无法接入 PDF/Word/HTML 等常见资料。

**证据**
- `rag/index.py:139-156`：`get_index()` 每次 `DocLoader.load()` 全量读取并计算 digest，索引重建在全局锁内。
- `rag/index.py:30-31`：后缀过滤仅 `.txt`/`.md`。
- `rag/index.py:90-114`：文档变化时重建分块与 BM25。

**修复建议**
1. 建立文件指纹缓存（mtime/size + 快速内容哈希），只读取变更文件；未变化时复用索引。
2. BM25/分块按文件增量维护；重建放在后台线程，避免搜索请求等待。
3. 增加可选加载器（pdf/docx/html），依赖缺失时降级并提示，不阻塞默认路径。
4. 测试：1000 文件语料二次搜索读文件数为 0；改动单文件只重算该文件。

**验收**
- 大语料二次搜索延迟显著下降；并发搜索不互相阻塞。
- `dev/tests/test_hybrid_rag.py`、`test_rag_search.py` 全过。

---

**修复说明** 每次全读语料造成 IO 放大。rag/index.py 按文件指纹复用正文哈希，分块缓存，未变索引绕过重建锁；rag/lexical.py 缓存分词。新增 HTML 和可选 PDF/docx 加载器，pyproject.toml documents extra，uv lock/uv sync --locked 已同步（新增 lxml/pypdf/python-docx 仅可选）。test_bug12_index.py 验证 1000 文件第二次零读取、单改只读一文件及 HTML 去脚本。最小版本保持变化请求同步更新全局 BM25 IDF，不使用陈旧后台索引。 验证：`uv run pytest dev/tests/test_bug12_index.py dev/tests/test_hybrid_rag.py dev/tests/test_rag_search.py -q（31 passed）；uv sync --locked 成功`。

## BUG-13 RAG 阈值未校准、质量回归默认跳过

**级别** P1　**状态** 已修复（说明见下）　**文件** `config.py`、`rag/tool.py`、`dev/tests/test_rag_integration.py`

**症状**
- strict/normal/loose 阈值是原始 logit 拍值，与模型强相关，默认值可能过度过滤或放水。
- strictness 集成测试默认不执行，阈值变化没有回归门。

**证据**
- `config.py:87-90`：`RAG_THRESHOLD_*` 默认 0.0/-2.0/-10.0。
- `dev/tests/test_rag_integration.py:48`：`test_real_strictness_gates_only_after_reranking` 属于 integration 标记，默认跳过。
- `docs/rag-engineering-2026-09-27.md` 自述「不使用 qrels，不校准或评价阈值」。

**修复建议**
1. 在 `dev/rag/` 增加小规模标注集与校准脚本，输出建议阈值与 hit@k / MRR 报告，存入 `docs/`。
2. 增加可选质量回归（integration 标记），阈值变更必须附带报告。
3. 可选：查询改写/多查询（LLM 生成变体后 RRF 融合），作为配置开关。
4. RAG 结果增加引用编号，提示词要求「只依据引用作答」。

**验收**
- 固定 qrels 上的可复现报告；阈值校准命令写入 README。
- 不改变默认路径的性能与测试稳定性。

---

**修复说明** 原始 logit 未校准且无质量门。新增 dev/rag/quality.py 与固定公开 qrels 清单：5 校准/5 留出、343 文档候选池，分开输入输出，文档去重指标、只用校准划分选阈值。真实链路已跑完，四阶段 Top3 和指标见 docs/rag-quality.md；默认阈值留出 hit@10/MRR 均 0.8，建议阈值未提高质量，默认保持不变。新增离线指标/划分测试与可选 integration 质量门；提示词要求引用编号作答。无新运行配置，测试入口 RAG_QUALITY_CORPUS。 验证：`uv run pytest dev/tests/test_bug13_quality.py -m "not integration" -q（3 passed, 1 deselected）；dev.rag.quality 真实 10 查询完成`。

## BUG-14 记忆仅手工、无上限、全量注入、不可检索

**级别** P1　**状态** 已修复（说明见下）　**文件** `core/storage.py`、`core/sessions.py`、`core/cli.py`

**症状**
- 只有 `/remember` 手工写入；记忆整段拼进 system，无相关性筛选。
- 无去重、无容量管理、无检索；跨会话完全隔离（当前为设计选择，但缺共享选项）。

**证据**
- `core/storage.py:161-164`：纯追加。
- `core/sessions.py:225-231`：整份记忆注入。
- `core/cli.py:311-318`：`/remember`、`/memory`、`/forget` 为唯一入口。

**修复建议**
1. 回合结束异步提取记忆候选（可配置开关，默认关闭），去重后写入。
2. 注入改为按相关度 top-k（复用 embedding/BM25），无相关时只注入摘要。
3. 增加容量上限、条目格式（时间/来源/标签），`/memory list|rm <id>`。
4. 跨会话共享作为显式开关，默认保持隔离。

**验收**
- 记忆注入 token 受控；相关记忆可被召回；旧测试兼容。

---

**修复说明** 记忆全量注入且无条目管理。新增 core/memory.py 兼容旧文本、结构化条目、BM25 top-k 和注入预算；core/storage.py 去重/容量/删除/显式共享，core/cli.py 增加 list/rm 与删除确认；core/sessions.py 可选后台候选提取，需用户 /remember 采纳，不自动改变有效记忆。新增 MEMORY_TOP_K=4、MEMORY_INJECT_CHARS=1200、MEMORY_AUTO_EXTRACT=False、MEMORY_SHARED=False。test_bug14_memory.py 覆盖去重删除和相关召回预算，旧隔离与记忆限制测试继续通过。 验证：`uv run pytest dev/tests/test_bug14_memory.py dev/tests/test_bug01_budget.py dev/tests/test_sessions.py dev/tests/test_cli.py -q（22 passed）`。

## BUG-15 单模型无 fallback、无 token/成本计量

**级别** P1　**状态** 已修复（说明见下）　**文件** `config.py`、`core/llm.py`、`core/sessions.py`

**症状**
- 主模型不可用时整轮失败，没有备用模型/服务。
- 只有字符指标，没有 token/成本统计，无法预算与优化。

**证据**
- `config.py:37-44`：单组 `API_KEY/BASE_URL/MODEL`。
- `core/llm.py:130-131`：metrics 仅有 `input_chars` 等字符数。
- `core/sessions.py:236-256`：超时/协议错误仅一次恢复，不切换模型。

**修复建议**
1. 支持 `MODEL_FALLBACKS`（同协议 endpoint/model 列表），在主模型多次瞬时失败后切换，并记录切换事件。
2. 使用 provider 返回的 usage 或近似估算，记录 token 与成本到 `last_run`。
3. 可选支持 `response_format` 结构化输出。
4. 测试：注入 5xx/超时后 fallback 成功；计量字段存在。

**验收**
- 主模型故障可自动切换（默认不改变单模型行为）。
- 会话记录可见 token/成本估算。

---

**修复说明** 单一模型瞬时故障无备用。core/llm.py 支持同协议 MODEL_FALLBACKS，在流开始前重试失败/超时后切换，开始输出后不重放；独立客户端关闭与会话 ContextVar 路由指标。记录 provider usage 或含 schema 的近似 token 与配置价格成本（未知为 null）。新增 MODEL_FALLBACKS=[]、MODEL_STREAM_USAGE=False、INPUT_COST_PER_MILLION=0、OUTPUT_COST_PER_MILLION=0。test_bug15_fallback.py 注入主模型连接错误验证备用成功，并验证 provider usage/费用。 验证：`uv run pytest dev/tests/test_bug15_fallback.py dev/tests/test_llm_stream.py dev/tests/test_llm_retry.py dev/tests/test_bug05_config.py -q（20 passed）`。

## BUG-16 评估 prompt 无长度控制

**级别** P1　**状态** 待修复　**文件** `rag/assess.py`

**症状**
- 多轮 RAG 上下文拼接后评估 prompt 可能超模型输入限制或超时。
- 评估失败虽不影响回答，但会丢失质量信号。

**证据**
- `rag/assess.py:93-97`：contexts 全量 `"\n---\n".join()`。
- `core/sessions.py:196-199`：评估受 `ASSESS_TIMEOUT` 约束，无输入预算。

**修复建议**
1. 评估输入设置总字符上限与单条截断，超限时保留头部与命中数说明。
2. 评估结果可选结构化（JSON）解析，失败降级为文本。
3. 测试：大上下文评估不超时、不报错。

**验收**
- 超大检索上下文下评估仍可完成或明确降级，不影响回答。
