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
- [x] P1：PERF-04 内存准入与正文磁盘缓存已实现（全量模型 RSS 上限未验收）
- [x] P1：PERF-09 推理并发隔离（响应隔离通过；严格 p95 不增目标未达到）
- [x] P1：PERF-10 稳定前缀与工具子集
- [x] P1：PERF-11 辅助模型与后台摘要
- [x] P1：FREE-01 命名只读工具根
- [x] P1：FREE-04 权限档位与范围白名单
- [x] P1：FREE-07 多知识库与过滤
- [x] P1：FREE-08 显式记忆管理
- [x] P1：FREE-09 headless
- [x] P1：FREE-10 分支与重发
- [x] P2：FREE-02 网络与长任务设计、实现
- [x] P2：FREE-06 会话预算设计、实现
- [x] P2：FREE-11 后台任务与子代理设计、实现
- [x] P2：FREE-12 图片最小版本设计、实现（真实视觉服务未验收）
- [x] P2：FREE-14 可观测命令设计、实现
- [x] P2：FREE-15 平台诊断/锁适配与容器设计（Windows/macOS 未原生验收）

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

## PERF-11

`AUX_MODEL=` 默认主模型，同 BASE_URL；`AUX_MODEL_PARAMETERS={}` 可覆盖已支持采样参数。
`AUX_TIMEOUT=0` 沿用 ASSESS_TIMEOUT，`AUX_CONCURRENCY=0` 不增设辅助总额度；正整数启用独立辅助额度，主模型不占此额度。
`MEMORY_CONCURRENCY=0` 保留与评估共享，正整数分离记忆提取；`SUMMARY_CONCURRENCY=1` 限制后台摘要。
`CONTEXT_SUMMARY_BACKGROUND=false` 默认同步摘要不变；开启后缓存未命中不等待模型，保留真实淘汰说明，
后台按内容哈希生成有界摘要，后续相同淘汰历史可复用；新轮/停止/关闭取消任务，不把摘要写入原始对话。
不同内容键不会错用旧摘要；后台失败不阻断对话。代价是首次请求缺少该摘要，不能宣称语义质量等价。
`AUX_INPUT_COST_PER_MILLION=0`、`AUX_OUTPUT_COST_PER_MILLION=0`：美元/百万 token，未知；同主模型可继承主单价。
费用预算启用时不同模型未知价格在发送前拒绝；所有辅助调用沿用账本，没有预算/确认豁免。无新依赖、未下载新模型。

真实既有 deepseek-v4.1-flash + 固定合成超长历史，前/后各一次：
- 前台摘要准备 **3.373845 → 0.002345s**。
- 摘要真实完成 **3.373847 → 5.304836s**；生成本身没有变快，本轮网络调用反而更慢。
- 前台状态 applied → pending，等后台完成后第二次准备均 applied，真实摘要缓存各一项。
命令：`THINKING_MODE=disabled uv run python -m dev.perf.summary --output /tmp/agent-perf-results/perf11-{before,after}.json`，
after 显式 `CONTEXT_SUMMARY_BACKGROUND=true`。未接入另一个廉价模型，不虚构跨模型费用或摘要质量优势；SDK 入参测试验证配置路由。
新增失败测试先确认旧实现固定模型/超时且无调度接口；补充主/辅助不互锁、未知价格拒绝、后台缓存复用测试。
`uv sync --locked` 成功，移除可选 ann；完整 `uv run pytest -m 'not integration' -q`：**245 passed, 2 skipped, 3 deselected，8.16s**。
两项跳过为未装 ann 的真实 HNSW 测试，其默认精确/回退测试仍执行；此前 extra ann 原生测试已通过。

## FREE-01

`TOOL_ROOTS={}` 默认只有会话工作区；显式示例 `{"docs":{"path":"docs","read_only":true}}`。
配置路径在启动时相对仓库规范化，名字仅小写字母/数字/连字符/下划线；当前额外根只支持 read_only=true。
文件工具用 `@docs/a.md`、`list_files('@docs')`、`glob('@docs/**/*.md')`；`rag_search(source='@docs')` 检索该根。
普通路径仍相对原工作区，不接收主机绝对路径；`@workspace/` 在启用命名根时显式引用工作区。
只读根每次检查存在/根路径未改变，目录边界独立校验；../ 与外部 symlink 拒绝。
所有写工具在路径解析时声明写意图；move 源也需可写，copy 可从只读根复制到工作区但仍需确认/配额/原子发布。
RAG 使用显式根参数与独立磁盘词法命名空间，不修改全局 DOC_DIR，命名根扫描排除越界链接。
额外根不挂载给命令沙箱；没有新增额外可写根。读操作及拒绝沿用审计，无新依赖。

12 项新测试先失败，文件/扩展/命令针对性回归 **63 passed, 1 deselected，2.14s**。
真实 `uv run python dev/ci_sandbox_probe.py` 通过，额外目录在命令内不可见；不是模拟 bwrap 输出。
真实模型：`RUN_RAG_INTEGRATION=1 uv run pytest dev/tests/test_free01_roots.py -m integration -q`：**1 passed，9.78s**。
使用原有知识库的临时副本，校验命名根完整 RAG 和外部链接排除，不以该工程用例宣称检索质量提升。

## FREE-04

`TOOL_PERMISSION_POLICY=standard` 默认逐次确认；readonly 拒绝写路径/风险操作，trusted 仅采用用户显式规则作为预先确认。
`TOOL_PERMISSION_RULES=[]`：Pydantic 校验，文件规则如 `{"tool":"create_file","path_prefix":"notes"}`，
命令规则如 `{"tool":"run_command","command_prefix":["printf"]}`。无规则/未匹配仍逐次确认；不支持隐式全部批准。
文件规则比较解析后的工作区相对路径组件（notes 不匹配 notes-other），命令按 argv 比较且排除 shell 组合、插值、重定向、多行。
规则只决定确认方式，不放宽路径、额外根只读、Bubblewrap、断网或资源限制。匹配放行审计 decision_source=trusted_rule 与规则序号。
`/policy readonly|standard|trusted` 持久化当前会话，`/policy default` 恢复环境配置；任务运行中不能更改。
没有启用 `/approve all` 这种整轮不限范围授权；本项提供可完整使用的档位与逐工具范围规则。

先失败测试复现无 readonly 约束/无规则/无持久化接口；文件/命令针对性 **63 passed, 1 deselected**，其中真实沙箱执行 printf 规则。
完整 `uv run pytest -m 'not integration' -q`：**263 passed, 2 skipped, 4 deselected，8.26s**。
随后补充两会话 readonly/trusted 并发隔离，`uv run pytest dev/tests/test_free04_permissions.py -q`：**6 passed**。
无新依赖；权限变更由用户 CLI/配置完成，模型工具参数不能自行提升档位。

## FREE-07

`RAG_SOURCES={}`：命名知识库到 path/可选 thresholds 的映射，路径只读，不自动导入或修改数据。
`source=''` 仍使用 DOC_DIR；`source='manual'` 选择配置库；`@名称` 保留 FREE-01 只读根。
每库可配置 strict/normal/loose 三档阈值且必须递减；未提供沿用现状，不自动校准阈值。
模型仍共享全局配置；不实现 URL 导入、HyDE 或按库热切换模型，避免请求间修改全局配置。
工具增加 `top_k`（默认 None 按 breadth，显式上限 `RAG_MAX_TOP_K=50`）与带时区的 `updated_after`。
修改时间指文件 mtime，不是文档正文的发布日期；仅过滤请求跟踪 mtime 并使时间变化失效，无过滤默认元数据不变。
时间条件在两路候选截断前应用；过滤向量查询使用 exact，BM25 在全体匹配中筛选再截断，可能增加查询开销。
索引构建按显式根隔离词法文件，不切换全局 DOC_DIR；并发库同名文档不串内容。无新依赖。

新增四项失败测试先观察到缺参数/时间指纹/范围校验；补充双库并发与持久倒排隔离。
`uv run pytest -m 'not integration' -q`：**269 passed, 2 skipped, 5 deselected，9.10s**。
真实本地模型：`RUN_RAG_INTEGRATION=1 uv run pytest dev/tests/test_free07_sources.py dev/tests/test_rag_integration.py -m integration -q`：**3 passed，17.36s**。
真实工程用例使用原知识库临时副本，验证 top_k/命名库/时间过滤；原有四阶段/阈值后置测试保留，不是新质量评测或阈值搜索。

## FREE-08

`ENABLE_MEMORY_MANAGEMENT=false` 默认旧路径/隔离/容量/人工采纳不变。开启后可用：
- `/memory scope session|project|global|default`：选择并持久化命名空间；default 沿用 MEMORY_SHARED 原配置。
- `/memory add {"text":"事实","tags":["project"],"source":"manual","expires_at":"2100-01-01T00:00:00Z"}`。
- `/memory search 关键词` 或 JSON `{"query":"关键词","tags":["project"],"source":"manual","include_expired":false}`。
- `/memory edit ID {"text":"修订内容"}`，展示变更后 `/yes` 才原子更新；`expires_at:null` 可取消到期设置。
session 使用会话文件，project 在同一状态目录内按工作区根哈希分组，global 为同一状态目录共享，**不是跨状态目录/跨进程全局服务**。
scope 不复制或迁移旧记忆，切换仅改变当前会话读取目标；/remember、删除、清空和导出均使用所选空间。
开启管理后到期/到期元数据无效的条目不注入、不出现在默认搜索，但原文保留，过期条目仍计容量，不自动删除。
64 条与 MEMORY_MAX_CHARS 上限保持；标签最多 8 个、每个 32 字符，序列化元数据另有有界校验。
新管理写入附记忆审计（不记录正文），沿用原子写与文件锁；未实现自动采纳或时间衰减排序，不改变人工采纳语义。
先失败测试覆盖缺少命名空间、到期、搜索/编辑；补充 CLI 编辑拒绝/确认、导出/恢复与开关隔离。
验证：`uv run pytest dev/tests/test_free08_memory_management.py dev/tests/test_bug14_memory.py dev/tests/test_cli.py -q`：**10 passed**；
完整 `uv run pytest -m 'not integration' -q`：**274 passed, 2 skipped, 5 deselected，8.38s**。无新依赖。

## FREE-09

显式 `uv run main.py --prompt "任务" --json --state-dir <目录>` 单次运行；不创建 TUI，不读取 stdin，复用 SessionManager 全链路。
`--session <ID 或唯一前缀>` 继续已保存会话；无 --prompt 仍使用原 CLI。没有 --yes-all。
headless 默认 standard 且确认立即返回拒绝，即使环境/已保存会话是 trusted 也不自动继承；
仅显式 `--policy trusted` 才使用已有的范围预先确认规则，未匹配仍拒绝，全部工具配对/审计/隔离/原子收尾保留。
JSON stdout 仅一个对象，第三方 stdout 诊断重定向 stderr；已知配置密钥在结果与日志格式化中隐藏。
字段 version/session_id/turn_id/status/exit_code/messages/metrics/error/permission_policy；messages 仅本轮，完整历史仍持久化。
退出码 0 正常结束、2 参数/配置错误、3 检查点未完成、4 执行失败、130 取消；拒绝工具的具体原因在配对工具结果中。
参数错误在 stderr 返回信息，不伪造成功 JSON。仍保持状态目录单写者锁，没有 HTTP/API/跨进程共享服务。

新增失败测试后实现；文本/恢复、无 TTY 写拒绝、检查点、取消持久化、已知密钥隐藏与真实子进程 SIGINT 测试：**6 passed**。
真实既有服务调用：临时状态目录执行 --prompt/--json，stdout 可解析为唯一对象、status=idle、exit_code=0、2 条本轮消息/1 次模型调用。
原始结果 `/tmp/agent-headless-smoke-result.json`；没有替换模型输出或执行用户数据目录写入。
完整 `uv run pytest -m 'not integration' -q`：**280 passed, 2 skipped, 5 deselected，10.29s**。无新增配置变量/依赖（显式 CLI 参数开启）。

## FREE-10

`ENABLE_SESSION_BRANCHES=false`；开启后 `/branch [消息编号]` 创建截至该消息的独立快照（system=0，默认末尾），
`/resend 编号 新文本` 在新分支替换 user 输入并运行，`/retry` 在新分支重发最后一个用户问题。
原消息日志和工作区不改写；前缀验证拒绝孤立/重复/未完成工具配对，不补造工具结果。
新分支总是空工作区，系统说明明确旧工具结果属于原工作区；不提供共享引用或自动工作区复制。
模型预设/工具子集/权限继承；所选记忆复制成独立会话快照，避免 MEMORY_SHARED 别名使新分支反向修改原记忆。
独立 session 记忆快照不依赖 ENABLE_MEMORY_MANAGEMENT；原 global/project 管理仍需要其开关。
分支记录 parent_id/through，侧栏展示来源；忙会话拒绝分支，快照期间禁止提交/修改参数，线程收尾后才关闭存储。
保存失败只清理本次新建未发布的随机 ID 目录，不能覆盖旧目录。没有新依赖。

先失败测试覆盖缺少分支/重发接口；补充前缀配对、原数据不可变、并发分支持久化、共享记忆隔离、关闭收尾和 CLI 命令。
完整 `uv run pytest -m 'not integration' -q`：**286 passed, 2 skipped, 5 deselected，10.20s**；
随后 CLI 分支/重发专项补充：`uv run pytest dev/tests/test_free10_branches.py -q`：**7 passed**。

## FREE-02（P2：联网与长命令）

设计先追加于自由度报告，再分别运行失败测试（联网 3 failed、后台任务 3 failed）并实现。
联网采用 Unix CONNECT 网关及私有 loopback 中继，保持 `--unshare-all`、只读系统与仅工作区可写。白名单精确域名、443、公网 DNS 验证及固定 IP 连接；不支持明文 HTTP。每个联网命令强制确认并审计，trusted 不能替代。白名单若包含公共代理服务，相当于用户信任该服务的转发能力，宜仅配置目标服务域名。

| 配置 | 默认 | 含义 |
|---|---|---|
| COMMAND_NETWORK | off | allowlist 才允许工具参数 network=true |
| COMMAND_NETWORK_ALLOWLIST | [] | 小写 ASCII 精确域名，国际域名用 punycode |
| COMMAND_NETWORK_MAX_BYTES | 8388608 | 每命令代理累计双向字节上限 |
| COMMAND_NETWORK_MAX_CONNECTIONS | 4 | 每命令累计代理连接上限 |
| COMMAND_NETWORK_TIMEOUT | 5 | 连接/传输时限（秒） |
| ENABLE_COMMAND_JOBS | false | 注册 start_command_job/job_status/job_logs/cancel_command_job |
| COMMAND_JOB_MAX_SECONDS | 300 | 后台命令墙钟上限；CPU/内存/文件限额不变 |
| COMMAND_JOB_CONCURRENCY | 1 | 后台执行线程数 |
| COMMAND_JOB_MAX_ACTIVE | 16 | 运行与排队任务合计上限 |
| COMMAND_JOB_LOG_BYTES | 262144 | 每任务保存的 stdout/stderr 合并前缀字节上限 |

后台启动始终确认，返回 ID 后必须用 status/logs 观察真实结果；取消无需再次确认但记录审计。状态与日志在 state-dir/command-jobs/，按 owner 校验；退出取消并等待，重启标记 interrupted 而不重放。工作区写锁串行化后台命令和前台写工具。仅主进程存活时执行，不是系统服务；无自动重试、不开放宿主 venv/包管理挂载。

验证：`uv run pytest dev/tests/test_free02_jobs.py dev/tests/test_free02_network.py dev/tests/test_command.py -q`：26 passed。真实 Linux bwrap 网络探针：白名单 example.com HTTPS 返回 200/559 字节；未授权 python.org 返回 CONNECT 403；直接连接公网 IP 返回 errno 101（无路由）。结果 `/tmp/agent-perf-results/free02-network.json`，无外部请求正文入审计。新增模块均为标准库，不增加依赖。

## FREE-06（P2：会话预算）

`ENABLE_SESSION_BUDGETS=false` 默认沿用全局配置；启用后 `/config {"MAX_TOOL_ROUNDS":12,"MODEL_RECOVERY_LIMIT":0}` 或 `/config conservative|standard|aggressive|default`。
JSON 替换当前覆盖集，default 清空。预设仅缩放轮次和输出上限，其他值保持全局默认；每个会话持久保存、运行时禁止修改、分支继承。CLI 状态栏显示剩余模型轮次。仅用户 CLI 可设置，不增加模型修改配置工具；不需要写工具确认，不改权限或审计安全策略。

允许覆盖：MAX_TOOL_ROUNDS 1..256，MAX_TOOL_CALLS_PER_ROUND 1..32，MODEL_INPUT_CHARS 512..1048576，MODEL_INPUT_TOKENS 128..262144，MODEL_OUTPUT_CHARS 1..1048576，TOOL_TIMEOUT (0,3600] 秒，MODEL_RECOVERY_LIMIT 0..3。未覆盖项保留既有环境值；工具独立时限和会话时限取更小值（仅显式覆盖时）。新增全局 MODEL_RECOVERY_LIMIT 默认 1，0 禁止自动恢复；恢复不得重放工具。

采用 ContextVar，不修改全局配置，线程继承调用上下文。失败测试首先 4 failed；覆盖并行不同轮次、持久化、恢复 0/2、非法安全字段拒绝、实际上下文预算拒绝及 CLI。未新增依赖。

## FREE-11（P2：持久任务队列与一层子代理）

`ENABLE_AGENT_TASKS=false`，启用时必须同时打开会话预算；`AGENT_TASK_CONCURRENCY=2` 限制运行子代理数，`AGENT_TASK_MAX_ACTIVE=16` 限制运行+排队任务。无新增依赖。
新增 delegate/task_status/cancel_agent_task，CLI `/tasks` 查看最近 20 项、`/tasks cancel ID` 取消。delegate 必须逐次确认，任务文本最多 16000 字符，tool_names 显式取父会话子集，max_rounds 默认 6 且不得超过父会话，delay_seconds 默认 0、最多 86400。子代理不能继续委派或启动长命令。
子会话拥有空工作区、独立记忆与上下文；继承有效权限/模型/预算，费用归父会话账本，模型调用仍共用总并发限额。任务状态、错误、有限结果存于 state-dir/agent-tasks/；完整对话在对应子会话。查询工具通过正常工具结果配对返回，owner 不匹配拒绝；启动/完成/工具取消审计不含任务正文（确认审计仍含用户需审阅的详情）。
进程退出、父会话取消均停止子任务并等待写操作收尾；重启将未完成项标为 interrupted，不自动重放。延时调度只在主进程存活时有效；headless 结束时也会取消子任务，因此需在主任务内查询完成结果。cron/webhook/跨进程服务不在本版范围。
失败测试先运行 3 failed；之后测试独立会话、权限/工具/预算/成本归属、并发限制、取消、重启、确认拒绝和真实线程桥接。模型响应仅在单元测试中使用明确测试桩；未将测试桩当真实模型质量验收。

## FREE-12（P2：图片附件，服务端验收受限）

`ENABLE_IMAGE_INPUT=false`；`VISION_MODELS=[]` 必须显式列出已验证支持视觉的模型。安装 `uv sync --locked --extra vision`（可选 Pillow>=12,<13；当前依赖树已间接安装 Pillow）。执行 `/image 工作区相对路径 | 问题`，上传前强制确认图片摘要、模型与服务。未配置能力时显示提示，可继续文本对话。

| 配置 | 默认 | 含义 |
|---|---|---|
| IMAGE_MAX_BYTES | 5242880 | 原文件/标准化附件分别最多 5 MiB |
| IMAGE_MAX_PIXELS | 4000000 | 解码前像素上限 |
| IMAGE_MAX_PER_REQUEST | 4 | 单次请求历史内的图片数上限 |
| IMAGE_TOKEN_BUDGET | 4096 | 每图片预留 token 估计，纳入上下文/成本预算 |
| IMAGE_TOTAL_MB | 32 | 每会话附件目录容量，超限拒绝且不删除旧附件 |

仅 PNG/JPEG 普通文件，拒绝越界/动画；解码后转 JPEG、去元数据。state-dir/session-id/attachments/ 按 SHA256 原子保存；持久消息与导出仅含引用，不含 base64，迁移需同时保存附件目录。TUI 显示附件摘要；分支复制被引用附件。
只向原批准模型与服务上传；备用服务/模型变化时拒绝，需新会话重新确认。批准包含后续上下文向同一模型重新发送；不发给后台摘要/评估模型。预算为可配置保守预留，不代表真实视觉 token 计费，需按服务验证调整。音频/视频/屏幕采集仍不开放。
先运行失败测试 4 failed；验证格式/像素/路径、确认拒绝、引用持久化、预算、SDK payload 与分支/导出。真实视觉服务未配置，未运行识图质量与 provider 兼容验收；协议测试使用明确测试桩，不报告为真实模型成功。`uv lock` 与 `uv sync --locked` 成功，锁文件自动更新。

## FREE-14（P2：只读观测命令）

`ENABLE_OBSERVABILITY=false`，启用后 `/usage` 汇总当前会话保留轮次（非终生/账单总额），按 turn_id 去重；未知费用数量单列，任一未知时 total_cost_usd=null。模型用量区分 provider 与本地 estimate，不把估算冒充计费账单。
`/trace [轮次ID前缀]` 展示时间/状态、上下文计数、模型 usage 与工具名称/耗时/输出长度白名单。`TRACE_MAX_ROWS=20` 限制轮次与每轮明细行数，`TRACE_MAX_FIELD_CHARS=512` 限制单字段。消息正文、工具参数、图片数据及审计详情均不读取；已配置主/备用/检索/网页凭据展示前替换。脱敏限于已知配置密钥，不声称任意文本敏感信息识别。
两个命令不调用模型/网络、不需写入确认、不追加审计事件、不改变日志格式。指标可直接从懒加载元数据读取。JSONL 订阅及反馈持久化仍属后续设计。新增 3 项测试先失败，后验证去重、未知费用、脱敏/上限、无历史加载与 CLI；无新增依赖。

## FREE-15（P2：平台诊断与锁适配）

无新增配置/依赖。核心存储、向量缓存和 ANN 统一 `core.file_lock`：POSIX flock 行为保留，Windows 使用首字节 msvcrt 互斥，未知平台拒绝无锁运行。初始化锁异常关闭描述符；RAG 锁文件改追加打开避免截断锁字节。
非 Linux 命令在探测启动器前明确拒绝，提示 Linux/WSL2，不回退宿主执行。新增 `/platform`、`--platform` 只读说明，后者无需凭据/状态目录；静态信息始终标记隔离尚需实际探针验证。平台矩阵与 Docker/Podman 设计见 platform-design.md；容器后端未实现。
失败测试先 3 failed。真实 Linux 子进程竞争验证第二写者拒绝、解锁后成功；非 Linux 拒绝用平台模拟；Windows msvcrt 仅契约测试，不称原生验收。真实 `uv run python dev/ci_sandbox_probe.py` 另行执行。无 Windows/macOS 原生环境，完整文件/推理/终端兼容仍是明确限制。

### FREE-02 交付前复查

补充失败测试发现 `ipaddress.is_global` 对 IPv4/IPv6 组播也可能为真；代理改为只允许公网单播，额外拒绝保留/未指定/6to4/Teredo 过渡地址。先复现 2 failed，再运行命令/联网/任务专项 29 passed；实际白名单 HTTPS 复测 200/559 字节，结果 `/tmp/agent-perf-results/free02-network-final.json`。没有放宽任何出网条件。
