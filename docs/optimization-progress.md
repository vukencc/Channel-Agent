# 优化实施记录

按 P0 → P1 → P2 顺序执行；每项先基准或失败测试，再实现并独立提交。
用户数据目录保持不变，所有测量输出使用 `/tmp/agent-perf-results/`。
初始工作树只有用户未跟踪的报告与任务提示，不覆盖其问题描述。

## 任务清单

- [x] P0：PERF-05 会话懒加载
- [x] P0：PERF-06 上下文单次序列化
- [x] P0：PERF-07 配额节流
- [x] P0：PERF-08 审计句柄（补充优化与五次复测已达标）
- [x] P0：PERF-12 流式导出
- [x] P0：FREE-03 文件工具
- [x] P0：FREE-05 模型参数
- [x] P0：FREE-13 成本与速率预算
- [x] P0：FREE-16 提示词模板
- [x] P1：PERF-01 重排与缓存
- [x] P1：PERF-02 可选 ANN
- [x] P1：PERF-03 增量持久化 BM25
- [ ] P1：PERF-04 内存准入与正文磁盘缓存已实现；全量模型 RSS 上限未验收
- [x] P1：PERF-09 推理并发隔离（响应隔离通过；严格 p95 不增目标未达到）
- [x] P1：PERF-10 稳定前缀与工具子集
- [ ] P1：PERF-11 辅助模型
- [ ] P1：FREE-01 多工具根
- [ ] P1：FREE-04 权限策略
- [ ] P1：FREE-07 多知识库与过滤
- [ ] P1：FREE-08 记忆管理
- [ ] P1：FREE-09 headless
- [ ] P1：FREE-10 分支与重发
- [ ] P2：FREE-02 网络与长任务设计、实现
- [ ] P2：FREE-06 会话预算设计、实现
- [ ] P2：FREE-11 后台任务与子代理设计、实现
- [ ] P2：FREE-12 多模态设计、实现
- [ ] P2：FREE-14 可观测命令设计、实现
- [ ] P2：FREE-15 跨平台设计、实现

PERF-13/14 未列入本次阶段目标，暂不变更渲染与保存持久性策略。

## PERF-05

管理器与 `--list` 只加载元数据，读取消息或修改会话时才校验历史并恢复中断工具。
兼容 v1；旧版单文件必须解析，首次保存继续使用原迁移逻辑。未启用历史自动卸载或删除。
没有新增配置或依赖。`--list` 不再恢复/写入会话；切换或提交时恢复。

测量：`uv run python -m dev.perf.benchmark sessions --output /tmp/agent-perf-results/perf05-{before,after}.json`。
两个独立进程，各生成 100 个约 10 MiB 的 v2 会话，准备数据不计时；只测管理器构造。
耗时 **1.969569 → 0.003993 秒**，进程峰值 RSS 增量 **1016.246 → 0 MiB**（ru_maxrss，非绝对内存）。
首次查看目标会话仍需加载其消息；不声称完整 TUI 冷启动为 4 ms。

失败测试先观察到启动读取 3 份历史；修复后仅首次访问目标读取一份。
针对性回归：`uv run pytest dev/tests/test_perf05_lazy_sessions.py dev/tests/test_sessions.py dev/tests/test_cli_performance.py -q`。
环境说明：受限执行中异步唤醒停滞；授权在限制外运行同一测试通过，未改测试或安全行为。

## PERF-06

调用内保存每条消息的序列化/字符/token 计量；修改的工具参数单独复制，其他嵌套内容只读共享。
ASCII 计数使用标准库 C 编码实现；摘要输入在预算处停止序列化，不构造全量淘汰历史。
10,001 条混合中英文消息、3 次采样中位 **0.732597 → 0.053915 秒（下降 92.64%）**。
前后发送 27,810 字符、估算 15,426 tokens、淘汰 4,986 轮，完全一致。
命令：`uv run python -m dev.perf.benchmark context --output /tmp/agent-perf-results/perf06-after.json`。
验证：`uv run pytest dev/tests/test_context.py dev/tests/test_perf06_context.py dev/tests/test_bug08_summary.py dev/tests/test_sessions.py -q`。
新增测试先确认未修改消息序列化 9 次，优化后 1 次；原始记录不可变测试保持通过。
无新增配置/依赖；无模型调用，token 为本地估算，不是服务商账单。

## PERF-07

命令配额扫描按单调时钟节流，输出到达不再重复触发全目录遍历；开始、确认后、结束检查保留。
`COMMAND_QUOTA_INTERVAL=0.1` 保留既有采样频率；显式改 1 秒可进一步降 IO，但会扩大超额发现窗口。
单文件 rlimit、超额终止及审计均保留。配额仍是采样检测，不是文件系统硬配额。
真实 Bubblewrap 基准：3,000 个文件，100 次 8 KiB 输出，间隔 5 ms。
墙钟 **1.776154 → 0.582655 秒**；父进程 CPU **1.775906 → 0.132174 秒**；扫描 **104 → 7 次**，
扫描耗时 **1.771452 → 0.127973 秒**。默认参数下测量，无主机执行回退。
验证：`uv run pytest dev/tests/test_perf07_quota.py dev/tests/test_command.py dev/tests/test_bug04_limits.py -q`：26 passed。
测量命令：`uv run python -m dev.perf.benchmark quota --output /tmp/agent-perf-results/perf07-after.json`。

## PERF-08

使用最多 64 个 LRU 追加描述符，直接 os.write，不缓冲事件；线程锁避免行交错；每次检查 inode，兼容日志轮转。
复用 JSON 编码器；句柄淘汰/退出关闭，写入失败仍遵循既有告警行为。
`AUDIT_SYNC=false` 保持原内核写入语义（进程崩溃无用户态缓冲丢失，不承诺断电持久性）；true 每事件 fsync。
10,000 事件基准 **0.128443 → 0.081054 秒，下降 36.90%**。内容条数真实校验；首轮实现 0.105372 秒后进一步优化。
**报告中 ≥50% 的性能目标未达到，不标为完全验收。** 为保留轮转即时识别，没有省掉 inode 校验。
测试：失败先证明每事件 open 和没有 fsync；新增复用、轮转、同步、并发完整行、64 句柄上限回归。
验证命令：`uv run pytest dev/tests/test_perf08_audit.py dev/tests/test_file_crud.py dev/tests/test_command.py -q`。
基准：`uv run python -m dev.perf.benchmark audit --output /tmp/agent-perf-results/perf08-after.json`。

## PERF-12

v2 导出逐行读取已提交日志，逐消息输出 JSON/Markdown，fsync 后原子替换；损坏日志不发布部分文件。
v1 继续兼容，但旧单 JSON 格式本身仍需完整解析。峰值由最大单条消息决定，不承诺单条 100 MiB 消息的低内存。
100 MiB 日志（1,600 条各 64 KiB）：**0.938152 → 0.661944 秒**；tracemalloc 峰值分配 **400.817 → 0.356 MiB**；
输出均 **104,946,050 字节**。该数字是 Python 分配峰值，不是进程总 RSS。
基准：`uv run python -m dev.perf.benchmark export --output /tmp/agent-perf-results/perf12-after.json`。
失败测试证明旧实现必须全量 read_record；新增格式等价及损坏不发布测试。
验证：`uv run pytest -m 'not integration' -q`：**203 passed, 3 deselected**，5.71 秒。
`uv sync --locked` 成功；所有运行目录通过环境变量重定向 `/tmp/agent-optimization-validation/`。
无新增配置/依赖；未启用压缩格式。

## FREE-03

`ENABLE_FILE_EXTRAS=false`；设 true 后注册 Pydantic 参数模型的 mkdir/move/copy/stat/glob。
mkdir 可显式创建父目录；move/copy 支持普通文件，目标必须不存在，不隐式递归搬移目录。
move 使用 Linux renameat2(RENAME_NOREPLACE)，不支持时明确拒绝；copy 临时写入 + fsync + 原子无覆盖发布。
stat/glob 只读并审计；glob 不进入链接目录，有结果/字符上限。写操作检查路径、配额并逐次确认，确认后重新校验源和目标。
无新依赖；不增加删除目录能力。取消/拒绝、越界、目标竞争和配额测试均使用临时目录。
验证：`uv run pytest dev/tests/test_free03_file_extras.py dev/tests/test_file_crud.py -q`：30 passed。
本项功能测试不调用模型；不提供虚构性能指标。

## FREE-05

`MODEL_PARAMETERS={}`：可配置 temperature[0,2]、top_p(0,1]、正整数 max_tokens、parallel_tool_calls、
response_format（text/json_object）、tool_choice（auto/none/required）。默认不传额外字段，选择工具仍 auto。
`MODEL_PROFILES={}`：形如 `{"precise":{"model":"模型名","parameters":{"temperature":0.2}}}`。
`/model precise` 选择并持久化；`/model default` 恢复默认；当前任务运行时禁止切换。
模型名不同且无已知单价时成本显示未知；不会静默沿用主模型单价。配置不改变写入确认或工具权限。
不兼容这些 Chat Completions 参数的服务会明确报错，不自动删参数重试。不新增第三方依赖。
失败测试 → 参数范围/未知字段校验、并发 profile 隔离、保存恢复及实际 SDK 入参捕获。
验证：`uv run pytest dev/tests/test_free05_model_parameters.py dev/tests/test_llm_stream.py dev/tests/test_sessions.py -q`：31 passed。

## FREE-13

新增 `SESSION_COST_LIMIT=0`、`DAILY_COST_LIMIT=0`（美元）与 `MODEL_REQUESTS_PER_MINUTE=0`；0 表示不限。
在 SessionManager 调用域内覆盖主模型、重试/备用请求、摘要和后台评估；不同会话共享状态目录的 UTC 日额度与滚动 60 秒速率。
启用后原子持久化 `budget.json`，每个实际请求先预留输入估算 + 输出上限费用，锁内检查防止并发超发。
仅有 provider usage 时结算退还差额；中断/无 usage 保留估算，不把未知费用记零；重启保留预留。
费用上限要求目标模型正的输入/输出单价；未知价格阻止发送。不同 profile 模型无价时同样拒绝。
超限保存 checkpoint，不执行额外模型/工具请求；后台任务明确失败降级。默认关闭时不创建账本。
**这是本地估算/请求准入，不是服务商账单硬上限**：token 估算、服务商额外计费与隐藏推理可能有差异；本地 RAG CPU 不计美元。
状态与停止原因写入账本、会话和原有运行日志；不替代工具操作确认/审计。无新依赖。
验证：`uv run pytest dev/tests/test_free13_budgets.py dev/tests/test_llm_stream.py dev/tests/test_sessions.py -q`：27 passed。
新增持久化/预留、速率恢复、未知单价拒绝和完整会话发送前拦截测试。

## FREE-16

FILE_APPEND_CHARS=4000 同时注入提示词、append_file schema 和运行时校验；FILE_READ_CHARS 控制 schema 与默认分页。
RAG_RETRY_LIMIT=2 参数化提示词中的重试建议（不是新的强制重试循环）。SYSTEM_PROMPT_FILE 留空使用内置模板；
显式文件仅用于新会话，要求 UTF-8、非空且不超过 MODEL_INPUT_CHARS，读取失败明确退出，已有会话不改写。
默认两份提示词 SHA256 与改动前完全一致。任何自定义提示均不能关闭工具确认或沙箱。
新增子进程测试验证配置同时改变 schema/文本、文件缺失拒绝与默认快照。
完整离线回归：222 passed, 3 deselected（6.98s）；随后新增快照专项 3 passed。
验证命令：`uv run pytest -m 'not integration' -q`；`uv run pytest dev/tests/test_free16_prompt_templates.py -q`。
无新依赖。

## PERF-08 补充验收

继续保留每事件 inode 校验；缓存最多 64 组公共审计字段及秒级格式化时间，动态字段仍逐条编码和内核追加。
补充优化前/后单次 **0.076666 → 0.069936 秒**。随后扩大为 5 次、每次 10,000 事件并逐条核对编号的复测：
- 历史提交 3ae9a2a 的真实 audit/_audit_path 函数：0.200208、0.172234、0.156593、0.153286、0.145653s，中位 **0.156593s**。
- 当前实现：0.075105、0.049811、0.047309、0.044647、0.043507s，中位 **0.047309s**，减少 **69.79%**。
历史实现从 Git 解析并执行，不是模拟输出；未改写工作树回退。早期未达标的实测仍保留，不能混用单次与五次中位口径。
命令：`uv run python -m dev.perf.benchmark audit --baseline-ref 3ae9a2a --output /tmp/agent-perf-results/perf08-five-before.json`；
去掉 `--baseline-ref` 输出 after 文件。微基准为临时目录，无 fsync，不能推断 HDD/WSL 全部部署环境增益。
验证：`uv run pytest dev/tests/test_perf08_audit.py dev/tests/test_file_crud.py dev/tests/test_command.py -q`：49 passed。
新增秒边界与覆盖公共字段兼容测试；日志格式与每条写入安全语义保持。

## PERF-01

默认 CPU/fp32、50 候选、不缓存保持原状；显式配置设备/精度、breadth 候选映射和真实结果 LRU。
`RAG_RERANK_DEVICE=cpu`（cpu/cuda/mps/auto）；`RAG_RERANK_DTYPE=fp32`（fp16 仅 GPU）；
`RAG_RERANK_CACHE_SIZE=0`、`RAG_QUERY_CACHE_SIZE=0`（0 禁用）；`RAG_RERANK_BY_BREADTH={}`（未指定项沿用 TOP_N）。
缓存键含查询/段落内容哈希，模型实例由模型工件指纹/设备/精度隔离；长段落仍完整滑窗，不缩短内容。
无新模型或依赖：使用已存在 embedding-int8 与 mmarco-mMiniLMv2-L12-H384-v1；CPU、4 推理线程。
GPU 环境未实测，不宣称 GPU/fp16 性能或质量通过。候选缩减可能损失召回，基准保持 50 候选。

固定 C-MTEB/T2Retrieval 清单 343 文档、10 查询，2 轮真实完整检索（未改阈值）：
- 首次查询 p50 **5.998842 → 6.779045s**（本次反而变慢 13%，不宣称冷查询优化）。
- 第二轮 p50 **5.954295 → 0.001340s**，重排 **5.950797 → 0.000428s**。
- 独立冷索引构建 **16.766884 → 17.435558s**。
- 优化后显式 `RAG_RERANK_CACHE_SIZE=1024 RAG_QUERY_CACHE_SIZE=128`，热查询减少约 99.98%。
- 所有 20 份 vector/BM25/RRF/rerank 阶段及 selected_ids 前后完全相同。留出重排 hit@10=.8、MRR=.8、precision=.733333、recall=.64。
真实四阶段对比见 `docs/perf-rag-comparison.md`；全部原始 trace 在 `/tmp/agent-perf-results/perf01-{before,after}.json`。
基准命令：`uv run python -m dev.perf.benchmark rag --corpus .cache/rag/benchmark/corpus/corpus-00000-of-00001-8afe7b7a7eca49e3.parquet --output /tmp/agent-perf-results/perf01-after.json`；
设置 `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 EMBEDDING_MODEL_SOURCE=LOCAL EMBEDDING_LOCAL_PATH=.cache/rag/embedding-int8 RERANK_LOCAL_PATH=.cache/rag/reranker HF_HUB_OFFLINE=1 HF_HOME=/tmp/agent-perf-hf`。
验证：`uv run pytest dev/tests/test_perf01_rag_cache.py dev/tests/test_hybrid_rag.py -q`：28 passed。

## PERF-02

新增可选 `ann` extra（hnswlib 0.8.0）：`uv sync --locked --extra ann`；pyproject.toml/uv.lock 由 uv add 更新，uv lock --check 成功。
默认 `RAG_VECTOR_BACKEND=exact`；显式 ann 且子块数达到 `RAG_ANN_MIN_CHILDREN=10000` 时使用 HNSW。
`RAG_ANN_M=16`、`RAG_ANN_EF_CONSTRUCTION=200`、`RAG_ANN_EF_SEARCH=256`。
索引按真实向量内容/形状/构建参数指纹持久化，文件锁 + 临时文件 + fsync + 原子发布。
加载/搜索失败保留 exact 回退且写警告，trace.vector_backend 显示实际后端；基准若回退则失败，不计 ANN 通过。

100,000 × 384 维随机单位向量，固定 seed=20260928、20 个独立随机查询：这是索引内核性能/近似召回测试，**不是语义检索质量集**。
- exact：p50 **15.087ms**，p95 **15.758ms**，recall@50=1。
- ANN ef=256：p50 **1.554ms**，p95 **1.774ms**，recall@50=**0.227**，首次含建索引 **24.766s**。
- ANN ef=4096：p50 **14.002ms**，p95 **14.995ms**，recall@50=**0.932**，首次含建索引 **25.504s**。
中位/p95 不含第一条建索引请求。低深度在该高维随机数据上损失大，不能当作 exact 等价替换；实际语料需另测召回。
命令：`OPENBLAS_NUM_THREADS=1 uv run --extra ann python -m dev.perf.benchmark vector --output /tmp/agent-perf-results/perf02-after.json`，分别设 RAG_VECTOR_BACKEND 与 RAG_ANN_EF_SEARCH。
验证：`uv run --extra ann pytest dev/tests/test_perf02_ann.py dev/tests/test_hybrid_rag.py -q`：28 passed；含真实持久化、内容失效及缺依赖明确回退。
不安装 extra 时仅两项真实 ANN 测试标记依赖缺失；默认精确路径与回退测试照常运行，不伪报 ANN 通过。

## PERF-03

`RAG_BM25_PERSIST=false` 默认保留内存后端；显式开启后用 SQLite 文档指纹/词频/倒排表，仅更新变化条目的分词和 postings。
计数/总长度增量维护，查询时按当前快照计算正 IDF，不全量重写词典。WAL + 独立只读事务让旧对象继续读旧快照。
首次扫描仍需 O(N) 校对父块 ID/内容指纹；没有声称整个 RAG 重建已为常数时间，也未启用后台自动建库。

20,000 条确定性文本（词法算法性能工作负载，非语义质量集）：
- 首次构造 **1.258509 → 1.758474s**（增加持久化成本）。
- 单条增量更新 API **0.760130 → 0.000915s**（默认为全量重建，对照持久后端 with_updates）。
- 清空分词缓存后的重启 **0.730567 → 0.021477s**；匹配 ID=100，分数 24.38032100453355 完全一致。
基准：`RAG_BM25_PERSIST=true uv run python -m dev.perf.benchmark bm25 --output /tmp/agent-perf-results/perf03-after.json`。
单元验证：`RAG_BM25_PERSIST=true uv run pytest dev/tests/test_perf03_lexical_store.py dev/tests/test_hybrid_rag.py -q`：28 passed。
新增存储重启无重新分词、旧读快照、删词、与原公式/排名完全相同测试；保留所有现有混合检索测试。
不新增依赖，使用 Python 标准库 sqlite3。全部索引写临时目录。
真实完整链路：本地模型 + RAG_BM25_PERSIST=true + RUN_RAG_INTEGRATION=1，`uv run pytest dev/tests/test_rag_integration.py -q`：**2 passed，14.78s**，含四阶段结果与阈值仅后置过滤校验。

## PERF-04（实现与范围限制）

新增 `RAG_MEMORY_LIMIT_MB=0`，默认关闭。显式开启后，原文/父块/子块正文使用内容哈希 SQLite 存储，
分块与正文共享按字节淘汰缓存（预算四分之一，最多 64 MiB）；BM25 自动使用磁盘倒排表，嵌入按批读取正文。
Linux RSS 在加载、分块、索引、检索和重排边界检查，超预算明确拒绝。无新依赖，不改变工具确认或审计。
这是一项进程 RSS **准入检查**，包含其他会话与模型，不能限制检查间的临时/native 分配，不等同 cgroup 硬配额。

前后各独立进程，读取已有公开 T2Retrieval parquet 前 100,000 条，准备文件在子进程，不按检索表现挑选：
加载与分块峰值 RSS **460.250 → 337.570 MiB**，耗时 **13.255816 → 22.866322s**；父块均 **200,697**。
优化后预算 512 MiB。本测量不含全部语料嵌入/重排，不能宣称 10 万文档完整 RAG RSS 已验收。
命令：`RAG_MEMORY_LIMIT_MB=512 uv run python -m dev.perf.benchmark memory --corpus <已有公开 parquet> --output /tmp/agent-perf-results/perf04-after.json`。
默认回归：`uv run pytest -m 'not integration' -q`：**236 passed, 3 deselected，8.55s**。
启用预算 2048 的专项：`uv run pytest dev/tests/test_perf04_memory.py dev/tests/test_bug12_index.py dev/tests/test_hybrid_rag.py -q`：**31 passed，0.76s**。
真实本地模型集成：2048 MiB **1 passed / 1 failed**，第二项被真实 RSS 检查拒绝；未放宽检查或测试。
提高显式配置到 4096 MiB 后，同一 `RUN_RAG_INTEGRATION=1 uv run pytest dev/tests/test_rag_integration.py -q`：**2 passed，12.65s**。
因此 512 MiB 只是加载/分块场景结果，不是本地模型完整运行的推荐预算。
补充固定公开 343 文档/10 查询两轮实测：与 PERF-01 输出比较，**20 份四阶段全部字段及 selected_ids 完全一致**。
原始结果 `/tmp/agent-perf-results/perf04-rag-after.json`；同一 manifest SHA256，缓存配置保持 1024/128，未改阈值。

## PERF-09

`RAG_INFERENCE_CONCURRENCY=0` 保留原工具池；显式正整数启用专用线程池与 semaphore。
RAG 不占文件读取额度；线程超时后直到真实退出才释放推理额度；取消/关闭等待线程，ContextVar 预算与审计隔离保留。
不新增进程或重复模型，不提供不可终止线程的强制 kill 保证。无新依赖，无新增确认豁免。

先观察两项失败测试：RAG 占用文件槽导致探针超时，超时后的下一项推理立即进入；修复后会话/专项 **16 passed**。
真实本地模型，固定公开 343 文档与清单前四查询，关闭结果缓存，模型/索引预加载：
- 单请求四样本 p95（nearest-rank 最大值）：**6.969483 → 7.008098s**，增 0.55%，本轮未达到严格“不增加”。
- 四会话总墙钟：**19.351870 → 20.682083s**，增加 6.9%；不宣称吞吐提升。
- 四请求提交 50ms 后的文件探针：**18.102950 → 0.004723s**，解决共享额度导致的文件卡顿。
- 优化后并发配置 2；四份返回文本 SHA256 在串行/并行及优化前/后完全一致。
命令：`uv run python -m dev.perf.concurrency --corpus <固定公开 parquet> --output /tmp/agent-perf-results/perf09-{before,after}.json`，
分别设 RAG_INFERENCE_CONCURRENCY=0/2，OPENBLAS_NUM_THREADS=1、OMP_NUM_THREADS=4，模型路径同 PERF-01。
该测试只有四个查询，不能作为生产 p95 分布证明；需要更多样本才能判断小幅差异是否为噪声。

## PERF-10

`MODEL_STABLE_PREFIX=false`；开启后基础提示/工具协议保持前缀，记忆/轮次/省略说明/摘要/恢复说明在请求尾部。
`MODEL_TOOL_NAMES=null` 默认全工具，`[]` 无工具或工具名数组；`/tools read_file,rag_search` 持久化到当前会话，
`/tools all`、`/tools none` 显式覆盖。schema、上下文预算、成本预留和执行侧共用子集；未选调用拒绝并审计。
空子集不发送 tools/tool_choice；与 required 冲突明确报错。会话繁忙时不可更改；写操作仍需确认。
usage 保留输入总 tokens，另记录服务商提供的 cached_input_tokens；未返回时不虚构为 0，不注入专有缓存参数。

真实已配置 `deepseek-v4.1-flash` 服务，临时新会话、固定两句合成请求，不发送用户数据、禁止实际执行工具。
相同 tool_choice=auto/max_tokens=64/thinking disabled，对照全工具/原布局与两个工具/稳定前缀：
- 首轮 provider 输入 **4025 → 2006 tokens**；第二轮 **4040 → 2021 tokens**（减少 49.98%）。
- schema JSON **7941 → 2196 字符**；第二轮全请求 **9864 → 4154 字符**。
- 第二轮请求耗时 **2.850 → 2.533s**；仅两次请求，不能推断长期网络延迟分布。
- 优化后第二轮 provider 报告 **1920 cached tokens / 2021 总输入 tokens**；首轮 cached=0。
输入减少来自工具子集；缓存命中不等于总输入 tokens 减少，历史实现未记录缓存细项，不能编造命中提升比。
早期 tool_choice=none 探针只计 911/926 tokens（服务端忽略工具说明），保留 perf10-before-none.json，未拿来作为 auto 基线。
命令：`uv run python -m dev.perf.prefix --output /tmp/agent-perf-results/perf10-before.json`；
优化后加 `MODEL_STABLE_PREFIX=true` 与 `--tool-names read_file,rag_search`，其余配置相同。
新增三项测试先失败，补充并发选择隔离与摘要/淘汰后前缀稳定；完整回归 `uv run pytest -m 'not integration' -q`：**243 passed, 3 deselected，8.43s**。
无新依赖或新模型；本测试费用单价未配置，费用显示未知，不将其记为零。
