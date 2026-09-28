# 分阶段优化交付报告

基线 `7e7499b`，实现截止 `1f5af4f`。P0 → P1 → P2 已按顺序提交 28 个条目的实现；P2 每项先追加设计。**实现交付不等于所有性能/平台目标已验收**：PERF-04 全量推理内存、PERF-09 严格 p95 目标、真实视觉服务和 Windows/macOS 原生验收存在下述限制。PERF-13/14 不在本次指定阶段清单内，未扩展范围。

原报告的问题描述未改，只追加状态/设计/实测；AGENTS.md 未改。未对 `.agent/`、`crud_tests/`、`data/raw/`、`logs/`、`.cache/` 执行用户数据写入/删除；已有模型和公开语料仅用于读取。测试、索引与测量产物置于 `/tmp/`。用户原有三个未跟踪报告文件保持未跟踪，未扫入提交。

## 性能结果与提交

均为真实运行数字；每项先测基线再实现。不同测试的时间、内存口径不能混用。下表为各提交阶段测量，不宣称全部来自最终 HEAD 的同一轮跑分；完整命令、配置、失败记录见 [实施记录](optimization-progress.md)。

| ID / commit | 实现与测量方法 | 优化前 → 后 | 解释与限制 |
|---|---|---|---|
| PERF-05 `a68241f` | 元数据懒加载；100×10MiB v2 会话，独立进程测管理器构造 | 1.969569→0.003993s；峰值 RSS 增量 1016.246→0MiB | 非完整 TUI 冷启动；访问历史仍按需读取 |
| PERF-06 `acae936` | 浅拷贝、单次序列化；10001 消息，3 次中位 | 0.732597→0.053915s | 前后 27810 字符、15426 估算 tokens、4986 淘汰轮次相同 |
| PERF-07 `3ae9a2a` | 单调时钟节流配额扫描；真实 bwrap、3000 文件/100 次输出 | 墙钟 1.776154→0.582655s；CPU 1.775906→0.132174s；扫描 104→7 | 默认 0.1s，边界强制检查与 rlimit 保留 |
| PERF-08 `aa7309e`, `24c30f2` | 有界持久句柄/公共字段缓存；5×10000 事件中位 | 0.156593→0.047309s，减少 69.79% | 早期单次 0.128443→0.081054s 未达 50% 目标，原记录保留；无 fsync 微基准 |
| PERF-12 `44cc07e` | 流式原子导出；100MiB/1600 消息 | 0.938152→0.661944s；Python 分配峰值 400.817→0.355548MiB | 输出同为 104946050 字节；非总 RSS；v1 仍需整文件解析 |
| PERF-01 `c2205b5` | 重排设备/候选可配、有界缓存；公开 343 文档×10 查询×2 轮 | 重复查询 p50 5.954295→0.001340s；重排 5.950797→0.000428s | 冷建库 16.766884→17.435558s、首轮 p50 5.998842→6.779045s，未提速；20 份四阶段结果完全一致 |
| PERF-02 `572c8be` | 可选 HNSW；100000×384 固定随机向量、20 查询、seed=20260928 | exact p50/p95 15.087/15.758ms → ef256 1.554/1.774ms | recall@50 1→0.227；ef4096 为 14.002/14.995ms、recall=0.932；首次建库约 25s。仅内核性能，不是语义质量，默认 exact |
| PERF-03 `6420671` | SQLite 增量倒排/WAL 快照；20000 合成词法文档 | 单条更新 0.760130→0.000915s；重启 0.730567→0.021477s | 首建 1.258509→1.758474s；命中文档/分数相同；目录 fingerprint 仍 O(N) |
| PERF-04 `06c03eb` | 磁盘正文、字节 LRU、RSS 准入；100000 公开文档加载/分块 | 峰值 RSS 460.250→337.570MiB；13.255816→22.866322s | **不含完整嵌入/重排**；全量模型 RSS 未验收。准入是软限制；小语料 2048MiB 触发拒绝，4096MiB 两项真实集成通过 |
| PERF-09 `a86051e` | 专用推理池；本地模型/公开清单前四查询、并发 4、推理槽 2 | 文件探针 18.102950→0.004723s | 单请求四样本 p95 6.969483→7.008098s（+0.55%）；四请求墙钟 19.351870→20.682083s（+6.9%）。响应隔离改善，**不称 p95/吞吐达标** |
| PERF-10 `49841d3` | 稳定前缀/会话工具子集；真实 deepseek-v4.1-flash 两轮合成请求 | 第 2 轮 provider 输入 4040→2021 tokens；schema 7941→2196 字符；2.850→2.533s | 优化后 cached=1920/2021，基线无缓存细项；不能推断长期延迟或缓存提升比例 |
| PERF-11 `c92d397` | 可选辅助模型/后台摘要；真实服务、固定长历史 | 前台准备 3.373845→0.002345s | 摘要实际完成 3.373847→5.304836s；仅移出前台。未实测更便宜模型/跨模型成本或质量 |

基准入口：`uv run python -m dev.perf.benchmark CASE --output /tmp/agent-perf-results/FILE.json`；CASE 包含 sessions/context/quota/audit/export/rag/vector/bm25/memory。RAG/内存传 `--corpus <公开 parquet>`；并发、前缀、摘要分别使用 `dev.perf.concurrency`、`dev.perf.prefix`、`dev.perf.summary`。原始 JSON 保存在 `/tmp/agent-perf-results/`，临时文件不入 Git。

## 功能结果与提交

FREE 条目以功能/边界测试验收，无性能前后值时标为不适用，不编造数字。新增测试分别位于 `dev/tests/test_freeXX_*.py`；每项均先观察失败，再实现。

| ID / commit | 交付行为 / 核心技术 | 边界与验证重点 |
|---|---|---|
| FREE-03 `12e3f2f` | mkdir/move/copy/stat/glob；Pydantic + register_tool、原子无覆盖发布 | 默认关闭；写前确认、越界/软链/目标竞争/配额 |
| FREE-05 `faa57c7` | 模型采样/tool_choice、命名 profile 与会话持久化 | 默认请求参数等价；非法参数拒绝，SDK 入参验证 |
| FREE-13 `f81966c` | 原子费用预留账本、滚动速率、超额 checkpoint | 每实际请求含重试/辅助调用；未知单价拒绝，未知 usage 保留预留，非账单硬上限 |
| FREE-16 `bf95bc4` | 提示词/schema/运行时共用配置、可选提示文件 | 默认提示 SHA256 不变；自定义提示不能绕过工具安全 |
| FREE-01 `f8a0bc1` | `@name/` 命名只读根，文件/RAG 统一边界 | 不挂载给命令，不开放额外可写根；真实跨根隔离验证 |
| FREE-04 `64e9746` | readonly/standard/trusted、路径/简单 argv 白名单 | 默认逐次确认；未匹配仍确认，规则放行有审计 |
| FREE-07 `04b165f` | 命名知识库、top_k、updated_after 候选前过滤 | 不自动改写语料；过滤在双路召回截断前，过滤向量路径用 exact |
| FREE-08 `7272e00` | 文件记忆命名空间、标签/来源/到期、确认编辑 | 不自动删除到期条目，不自动采纳，原子写与容量保留 |
| FREE-09 `c49fb07` | --prompt/--json/--session headless | 默认拒绝确认；真实服务单 JSON 与真实 SIGINT 退出 130 验证 |
| FREE-10 `a654e2e` | /branch、/resend、/retry；独立快照与配对截断 | 原消息/工作区不改写，新工作区为空，独立记忆快照 |
| FREE-02 `0f7d0e6`, `e54d21e` | 私有网络内 CONNECT 代理；持久长命令、日志/状态/取消 | 强制联网确认、公网单播白名单、固定 DNS IP；工作区写锁、重启不重放；真实 200/403/直连无路由 |
| FREE-06 `1511533` | /config 会话预算、ContextVar 隔离、有限恢复次数 | 默认覆盖关闭/恢复 1 次；不覆盖沙箱安全配置，持久化/并发/恢复 0 与 2 测试 |
| FREE-11 `4edf2b4` | 一层 delegate、持久有界队列/延时、/tasks 查询 | 子代理继承权限/工具子集/预算，费用归父会话；取消级联；进程退出或重启不自动继续 |
| FREE-12 `135525c`, `1f5af4f` | /image；有界 PNG/JPEG 解码、去元数据、确认附件、请求时展开 | 引用纳入预算，重发保留附件，绑定批准模型/服务；SDK 协议测试通过，**真实视觉服务未验收** |
| FREE-14 `61598ec` | /usage、/trace；保留窗口聚合/字段白名单/脱敏 | 只读元数据、不加载正文；未知费用 null，行数/字段限额 |
| FREE-15 `445ab2c` | 平台文件锁、/platform/--platform、非 Linux 明确拒绝命令 | Linux 实测；Windows 锁仅契约测试、macOS 未原生验收；容器后端仅设计 |

## 真实 RAG 数据与阶段对比

复用冻结公开清单 `dev/rag/inputs/quality.json`（343 文档、10 查询，5 条留出）；SHA256 `a7ccf1c1031a8b3d74aa25f9f3c3b14e1c0b521059831ed32276e4b3cc7c22c6`。未重新挑选有利查询或调整阈值。合成数据只用于隔离会话/序列化/词法/向量内核性能，不用于宣称 RAG 质量。

向量 → BM25 → RRF → 重排的真实结果、原始 doc_id/score 样例与指标见 [四阶段对比](perf-rag-comparison.md)。优化前后 20 份 trace 的 ID、顺序、分数、原文、偏移及 selected_ids 一致；内存模式追加同样公开池复测也保持一致。vector/BM25/RRF 的未过滤 hit@10 均为 1；最终阈值后重排 hit@10/MRR 为 0.8/0.8，precision=0.7333、recall=0.64。过滤口径不同，不将它们混作同一指标提升。

本轮复用已有 `BAAI/bge-small-zh-v1.5` 本地 ONNX 与 `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` 重排模型；没有新增必选模型。辅助/视觉模型是显式配置入口，未下载或虚构已验证的新模型。

## 最终验证

所有运行状态、审计与索引目录重定向 `/tmp/agent-optimization-validation/`，UV 缓存使用 `/tmp/agent-uv-cache`。

| 验证命令 | 实际结果 |
|---|---|
| `uv sync --locked` | 成功，默认依赖恢复；可选 hnswlib 被移除 |
| `uv run pytest -m 'not integration' -q` | **319 passed, 2 skipped, 5 deselected，10.69s** |
| `uv sync --locked --extra ann --extra vision` + `uv run --extra ann --extra vision pytest dev/tests/test_perf02_ann.py dev/tests/test_free12_images.py -q` | **9 passed**；真实 native HNSW 已运行，非模拟成功 |
| 本地模型、`RUN_RAG_INTEGRATION=1`，执行 test_rag_integration/test_free01_roots/test_free07_sources 的 `-m integration` | **4 passed, 18 deselected，18.27s**；含真实完整链路、只读根、多知识库过滤 |
| `uv run python dev/ci_sandbox_probe.py` | 真实 Bubblewrap + prlimit 隔离通过 |
| 真实网络代理探针 | example.com HTTPS 200/559 字节；非白名单 CONNECT 403；公网直连 errno 101；收紧地址检查后 HTTPS 再测成功 |
| `git diff --check` | 通过 |

默认两项 skipped 是未安装 ann 的真实 HNSW 测试，已在显式 extra 环境单独通过。5 项 integration 从离线集排除；其中 4 项单独运行，公开质量程序本轮最终回归未重复运行（此前真实冻结池测量已保留），未开展新阈值评估。未运行 GPU、真实廉价辅助模型对照、真实视觉服务或远端 CI；不得将这些记为通过。

## 可选依赖与安装

- 新增 `ann`：`hnswlib>=0.8.0,<0.9`，用 `uv sync --locked --extra ann`；默认 exact 不需要它。
- 新增 `vision`：`Pillow>=12,<13`，用 `uv sync --locked --extra vision`；当前基础依赖树已间接包含 Pillow，extra 明确直接依赖契约。
- 原有 `documents`（pypdf/python-docx）保留；未新增 ONNX extra 或容器依赖。多 extra 可以同时传。
- `pyproject.toml` 与 `uv.lock` 已通过 `uv lock`/`uv sync --locked` 同步，未手工编辑锁文件。

## 新配置完整清单

共 67 项；默认保持原行为或显式关闭。修改配置需重启；会话级 `/model`、`/tools`、`/policy`、`/config` 另行持久化。下列“确认/审计”描述能力执行时语义，编辑 `.env` 本身不创建额外审批流程。

| 名称 | 默认值 | 含义 | 确认/审计 |
|---|---|---|---|
| RAG_INFERENCE_CONCURRENCY | `0` | 0 共用工具池；正数为专用推理并发 | 无新增确认豁免；保留原工具审计 |
| MODEL_STABLE_PREFIX | `false` | 启用稳定系统前缀布局 | 无新增确认豁免；保留原工具审计 |
| MODEL_TOOL_NAMES | `null` | null 全工具、[] 无工具、数组选子集 | 边界/子集校验；工具审计保留 |
| TOOL_ROOTS | `{}` | 命名只读根，不向命令挂载 | 边界/子集校验；工具审计保留 |
| RAG_SOURCES | `{}` | 命名只读知识库与可选三档阈值 | 边界/子集校验；工具审计保留 |
| RAG_MAX_TOP_K | `50` | 工具请求 top_k 上限 | 无新增确认豁免；保留原工具审计 |
| TOOL_PERMISSION_POLICY | `standard` | 默认逐次确认；readonly 拒写，trusted 使用显式范围规则 | 按有效策略确认；规则放行有审计 |
| TOOL_PERMISSION_RULES | `[]` | 路径或简单 argv 前缀预先确认规则 | 按有效策略确认；规则放行有审计 |
| AUX_MODEL | 空 | 空值沿用主模型，同 BASE_URL | 不豁免工具确认；预算账本/运行指标 |
| AUX_TIMEOUT | `0` | 0 沿用 ASSESS_TIMEOUT，正数为辅助请求秒数 | 不豁免工具确认；预算账本/运行指标 |
| AUX_MODEL_PARAMETERS | `{}` | 辅助调用采样参数覆盖 | 不豁免工具确认；预算账本/运行指标 |
| AUX_CONCURRENCY | `0` | 0 不新增辅助总并发限制 | 不豁免工具确认；预算账本/运行指标 |
| MEMORY_CONCURRENCY | `0` | 0 与评估共用，正数独立记忆提取额度 | 无新增确认豁免；保留原工具审计 |
| ENABLE_MEMORY_MANAGEMENT | `false` | 开启命名空间/搜索/到期/编辑 | 编辑/删除确认；记忆写入审计 |
| ENABLE_SESSION_BRANCHES | `false` | 开启独立分支、编辑重发和重试 | 用户显式命令创建分支，记录审计 |
| CONTEXT_SUMMARY_BACKGROUND | `false` | 缓存未命中时在后台生成摘要 | 无新增确认豁免；保留原工具审计 |
| SUMMARY_CONCURRENCY | `1` | 后台摘要并发上限 | 无新增确认豁免；保留原工具审计 |
| AUX_INPUT_COST_PER_MILLION | `0` | 辅助输入美元/百万 token；0 未知，同主模型可继承 | 不豁免工具确认；预算账本/运行指标 |
| AUX_OUTPUT_COST_PER_MILLION | `0` | 辅助输出美元/百万 token；0 未知，同主模型可继承 | 不豁免工具确认；预算账本/运行指标 |
| COMMAND_QUOTA_INTERVAL | `0.1` | 工作区运行中配额扫描秒数，边界强制扫描保留 | 无新增确认豁免；保留原工具审计 |
| AUDIT_SYNC | `false` | true 每事件 fsync；false 保留内核直接追加 | 无新增确认豁免；保留原工具审计 |
| ENABLE_FILE_EXTRAS | `false` | 注册 mkdir/move/copy/stat/glob | 写确认+审计；只读按原规则 |
| MODEL_PARAMETERS | `{}` | 支持的采样/tool_choice 参数覆盖 | 无新增确认豁免；保留原工具审计 |
| MODEL_PROFILES | `{}` | 命名模型/参数预设 | 无新增确认豁免；保留原工具审计 |
| SESSION_COST_LIMIT | `0` | 单会话本地估算费用限额，美元；0 不限 | 不豁免工具确认；预算账本/运行指标 |
| DAILY_COST_LIMIT | `0` | 状态目录 UTC 日估算费用限额，美元；0 不限 | 不豁免工具确认；预算账本/运行指标 |
| MODEL_REQUESTS_PER_MINUTE | `0` | 状态目录所有会话滚动 60 秒请求数；0 不限 | 不豁免工具确认；预算账本/运行指标 |
| FILE_APPEND_CHARS | `4000` | 追加工具/schema/提示共用字符上限 | 写确认+审计；只读按原规则 |
| RAG_RETRY_LIMIT | `2` | 提示词建议的检索重试次数，不是新增强制循环 | 无新增确认豁免；保留原工具审计 |
| SYSTEM_PROMPT_FILE | 空 | 新会话可选 UTF-8 系统提示文件 | 无新增确认豁免；保留原工具审计 |
| RAG_RERANK_DEVICE | `cpu` | cpu/cuda/mps/auto，非默认设备须显式选 | 无新增确认豁免；保留原工具审计 |
| RAG_RERANK_DTYPE | `fp32` | fp32 或显式 fp16 | 无新增确认豁免；保留原工具审计 |
| RAG_RERANK_CACHE_SIZE | `0` | 重排真实分数缓存条数；0 关闭 | 无新增确认豁免；保留原工具审计 |
| RAG_QUERY_CACHE_SIZE | `0` | 完整查询真实结果缓存条数；0 关闭 | 无新增确认豁免；保留原工具审计 |
| RAG_RERANK_BY_BREADTH | `{}` | 按 narrow/normal/wide 覆盖重排候选数 | 无新增确认豁免；保留原工具审计 |
| RAG_VECTOR_BACKEND | `exact` | exact 默认；ann 失败明确回退并记录实际后端 | 无新增确认豁免；保留原工具审计 |
| RAG_ANN_MIN_CHILDREN | `10000` | 达到此子块数才采用可选 ANN | 无新增确认豁免；保留原工具审计 |
| RAG_ANN_M | `16` | HNSW 图连接参数 | 无新增确认豁免；保留原工具审计 |
| RAG_ANN_EF_CONSTRUCTION | `200` | HNSW 构建搜索宽度 | 无新增确认豁免；保留原工具审计 |
| RAG_ANN_EF_SEARCH | `256` | HNSW 查询搜索宽度，影响速度与召回 | 无新增确认豁免；保留原工具审计 |
| RAG_BM25_PERSIST | `false` | 使用持久增量 SQLite BM25 | 无新增确认豁免；保留原工具审计 |
| RAG_MEMORY_LIMIT_MB | `0` | 0 关闭；正数启用磁盘正文/字节缓存/RSS 软准入，MiB | 无新增确认豁免；保留原工具审计 |
| COMMAND_NETWORK | `off` | off 默认断网；allowlist 才接受 network=true | 联网逐次确认+审计，trusted 不豁免 |
| COMMAND_NETWORK_ALLOWLIST | `[]` | 精确域名列表，仅允许公网单播 DNS | 联网逐次确认+审计，trusted 不豁免 |
| COMMAND_NETWORK_MAX_BYTES | `8388608` | 每命令累计双向网络字节 | 联网逐次确认+审计，trusted 不豁免 |
| COMMAND_NETWORK_MAX_CONNECTIONS | `4` | 每命令累计连接上限 | 联网逐次确认+审计，trusted 不豁免 |
| COMMAND_NETWORK_TIMEOUT | `5` | 代理连接/传输秒数上限 | 联网逐次确认+审计，trusted 不豁免 |
| ENABLE_COMMAND_JOBS | `false` | 注册后台命令启动/状态/日志/取消工具 | 启动逐次确认+审计；取消/完成审计 |
| COMMAND_JOB_MAX_SECONDS | `300` | 后台命令墙钟上限；原 CPU/内存限额不变 | 启动逐次确认+审计；取消/完成审计 |
| COMMAND_JOB_CONCURRENCY | `1` | 命令后台工作线程数 | 启动逐次确认+审计；取消/完成审计 |
| COMMAND_JOB_MAX_ACTIVE | `16` | 运行+排队命令总数上限 | 启动逐次确认+审计；取消/完成审计 |
| COMMAND_JOB_LOG_BYTES | `262144` | 每任务保留合并输出前缀字节上限 | 启动逐次确认+审计；取消/完成审计 |
| ENABLE_SESSION_BUDGETS | `false` | 启用 /config 会话局部预算覆盖 | 无新增确认豁免；保留原工具审计 |
| MODEL_RECOVERY_LIMIT | `1` | 自动恢复次数，可设 0..3；默认 1 | 无新增确认豁免；保留原工具审计 |
| ENABLE_AGENT_TASKS | `false` | 启用一层子代理队列；需同时开启会话预算 | 委派逐次确认+审计；子工具沿用规则 |
| AGENT_TASK_CONCURRENCY | `2` | 运行子代理上限 | 委派逐次确认+审计；子工具沿用规则 |
| AGENT_TASK_MAX_ACTIVE | `16` | 运行+排队子代理总数上限 | 委派逐次确认+审计；子工具沿用规则 |
| ENABLE_IMAGE_INPUT | `false` | 开启 /image 图片输入 | 图片上传逐次确认+审计，绑定原模型/服务 |
| VISION_MODELS | `[]` | 用户显式声明已验证视觉能力的模型名列表 | 图片上传逐次确认+审计，绑定原模型/服务 |
| IMAGE_MAX_BYTES | `5242880` | 原始与标准化附件分别限制的字节数 | 图片上传逐次确认+审计，绑定原模型/服务 |
| IMAGE_MAX_PIXELS | `4000000` | 解码像素上限 | 图片上传逐次确认+审计，绑定原模型/服务 |
| IMAGE_MAX_PER_REQUEST | `4` | 当前发送上下文的图片总数上限 | 图片上传逐次确认+审计，绑定原模型/服务 |
| IMAGE_TOKEN_BUDGET | `4096` | 每张图片预留 token 估计，非 provider 实测值 | 图片上传逐次确认+审计，绑定原模型/服务 |
| IMAGE_TOTAL_MB | `32` | 每会话附件目录上限，MiB | 图片上传逐次确认+审计，绑定原模型/服务 |
| ENABLE_OBSERVABILITY | `false` | 开启只读 /usage、/trace | 只读，无新增确认/审计事件 |
| TRACE_MAX_ROWS | `20` | 保留查询轮次及每轮明细行数上限 | 只读，无新增确认/审计事件 |
| TRACE_MAX_FIELD_CHARS | `512` | 观测字段字符上限 | 只读，无新增确认/审计事件 |

## 新模块与后续验收边界

核心新增模块按职责组织：`core/audit_writer.py`、预算/模型/工具策略模块、`core/command_jobs.py`、`core/agent_tasks.py`、`core/images.py`、`core/observability.py`、`core/file_lock.py`；RAG 新增向量后端、持久词法索引与正文磁盘存储；tools 中新增文件整理、任务控制、网络代理与中继。各自测试仍集中在 dev/tests/，基准输入与输出未混入生产模块。

后续不能略过的验收：10 万文档完整模型峰值 RSS；更大样本的 RAG p95/吞吐权衡；实际视觉服务兼容与图片 token 预算；Windows/macOS 原生文件锁/路径/终端；真实廉价辅助模型成本/质量。容器、音频/视频、HTTP/MCP、cron/webhook 保持设计边界，不宣称已实现。收到的异常字符尚缺少发生位置和触发操作，未确认其根因。
