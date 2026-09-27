# 缺陷修复交付报告

每项代码修复先失败测试，再最小修复和独立提交；BUG-19 按要求仅设计；所有运行数据使用临时目录。

## 交付范围与最终结果

基线 `93a7fed`。BUG-01–07 全部修复；BUG-08–16 按序实现可验证版本。
BUG-17/20 完成低风险工程改进，BUG-18 为协作取消（硬截止进程隔离仅设计），BUG-19 为 headless/API/MCP/多模态设计，未新增运行入口。
共 20 个独立 BUG 提交；验证中发现的同时间戳缓存、v2 诊断重放、配置模板完整性、预算计量复杂度问题已归入对应提交。

- 最终离线：`uv run pytest -m 'not integration' -q`，**192 passed, 3 deselected，6.15 秒**。
- 含真实模型及公开 qrels 的整套运行：**193 passed，102.22 秒**；此后补充两个离线断言并复跑上述离线集，三个真实集成项均已实际通过。
- 原有真实 RAG 完整链路与 strictness 后置过滤：单独运行 **2 passed，12.05 秒**；没有删除或弱化既有测试。
- `uv lock` / `uv sync --locked` 成功；默认环境 92 个包，可选 documents extra 新增 pypdf 6.19.0、python-docx 1.2.0、lxml 6.1.3。没有引入新模型。
- `uv run python dev/ci_sandbox_probe.py` 真实 Bubblewrap/prlimit 探测通过；受控 fork、大文件限制、失败关闭路径均有测试。远端 GitHub Actions 未触发。
- `.agent/`、`crud_tests/`、`data/raw/`、`logs/`、`.cache/` 的文件清单、大小和 mtime_ns 与工作开始时一致；测试工作区、状态、日志和索引写入临时目录。
- 用户原有 `dev/bug_report/task-prompt.md` 保留原样且未纳入提交；报告问题描述原文保留，仅变更状态及追加说明。

测试通过注入 fake model/流/故障复现控制流，实际执行 CRUD、命令隔离与持久化；60k 文件端到端用 fake model 驱动真实工具，并未把预设回答加入生产代码。
RAG 质量测试使用真实 BGE-small-zh-v1.5 INT8 与 mmarco-mMiniLMv2-L12-H384-v1、公开 T2Retrieval 原始 qrels，无自编相关性标注。
343 文档、10 查询（5 校准/5 留出）的结果：默认阈值留出 hit@10=0.8、MRR=0.8；建议阈值没有改善质量，因此未更改默认阈值。
四阶段真实 Top 3、指标、输入校验值与限制见 [RAG 质量报告](rag-quality.md)。平台边界见 [设计说明](platform-design.md)。

复现测试时先把 SANDBOX_DIR、AGENT_STATE_DIR、AUDIT_LOG、RAG_CACHE_DIR 指向新临时目录，避免使用真实用户目录。
真实集成另设置 RUN_RAG_INTEGRATION=1、EMBEDDING_LOCAL_PATH、RERANK_LOCAL_PATH、RAG_QUALITY_CORPUS 为已有模型和公开语料绝对路径。

## 提交索引

| BUG | 提交 |
|---|---|
| BUG-01 | `bb16336` |
| BUG-02 | `fbb6187` |
| BUG-03 | `74acbcc` |
| BUG-04 | `02731d9` |
| BUG-05 | `e7d8c90` |
| BUG-06 | `70b06ce` |
| BUG-07 | `a3f817e` |
| BUG-08 | `2461c28` |
| BUG-09 | `a68266a` |
| BUG-10 | `72ded53` |
| BUG-11 | `71cfd9e` |
| BUG-12 | `1305d38` |
| BUG-13 | `e064ba5` |
| BUG-14 | `43dd9b0` |
| BUG-15 | `928e069` |
| BUG-16 | `0228adc` |
| BUG-17 | `0c212a4` |
| BUG-18 | `f70e640` |
| BUG-19 | `c3938e1` |
| BUG-20 | 本报告所在的 BUG-20 提交（见 git log） |

## 任务清单

- [x] BUG-01
- [x] BUG-02
- [x] BUG-03
- [x] BUG-04
- [x] BUG-05
- [x] BUG-06
- [x] BUG-07
- [x] BUG-08
- [x] BUG-09
- [x] BUG-10
- [x] BUG-11
- [x] BUG-12
- [x] BUG-13
- [x] BUG-14
- [x] BUG-15
- [x] BUG-16
- [x] BUG-17（P2：低风险实现或设计）
- [x] BUG-18（P2：低风险实现或设计）
- [x] BUG-19（P2：低风险实现或设计）
- [x] BUG-20（P2：低风险实现或设计）

## BUG-01

根因是预算遗漏 schema 与无界记忆。core/context.py 统计 messages/schema/memory/extra；core/sessions.py 将预算不足转换为 checkpoint；core/storage.py 限制记忆追加与注入。新增 test_bug01_budget.py 三项回归，既有慢上下文测试仅适配参数签名，断言保持。配置 MEMORY_MAX_CHARS=4000。

验证：`uv run pytest dev/tests/test_bug01_budget.py dev/tests/test_context.py dev/tests/test_sessions.py -q（23 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-02

整轮持有信号量造成确认阻塞；core/sessions.py 改为仅模型调用占槽，评估独立池 ASSESS_CONCURRENCY=1。新增 test_bug02_slots.py 验证四个等待工具时第五会话可完成。

验证：`uv run pytest dev/tests/test_bug02_slots.py dev/tests/test_sessions.py -q（15 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-03

tools/web_search.py 移除导入时客户端，改用有超时及 2 MB 响应上限的 HTTP 流；默认逐次出网确认，查询完整展示、审计，缺 key 拒绝。参数限制 1–10 条、输出截断。新增 WEB_SEARCH_CONFIRM=always（可 off）、WEB_SEARCH_TIMEOUT=15 秒。测试覆盖拒绝不请求、参数越界、慢响应与大结果。

验证：`uv run pytest dev/tests/test_bug03_web.py dev/tests/test_tools.py -q（10 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-04

缺少资源限制。tools/sandbox.py 探测宿主 rlimit 能力与 prlimit，命名空间建立后设置 AS/CPU/FSIZE/NPROC；tools/command.py 执行前中后检查工作区大小，超额终止并审计，不删除文件。新增五项配置见 .env.example；轮询非硬磁盘配额、按进程/UID 限制边界详见 docs/sandbox.md。新增 test_bug04_limits.py 验证超额拒绝、缺启动器失败关闭、实际大文件受限。

验证：`uv run pytest dev/tests/test_bug04_limits.py dev/tests/test_command.py dev/tests/test_file_crud.py -q（48 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-05

config.py 增加集中启动校验并让数字解析错误携带变量名；core/cli.py 在创建状态目录前验证，main.py 捕获导入期配置错误并以退出码 2 输出中文提示。--list 无需模型密钥。新增 test_bug05_config.py 覆盖缺 key、非法范围、正确配置静默。无新配置。

验证：`uv run pytest dev/tests/test_bug05_config.py dev/tests/test_config.py dev/tests/test_cli.py -q（11 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-06

调试工具无条件注册与 stdout print 导致 schema 浪费及串屏。tools/__init__.py 显式 ENABLE_DEBUG_TOOL=True 才注册；tools/rag_search.py 改为日志长度指标。默认 DEBUG=False、ENABLE_DEBUG_TOOL=False。既有调试测试保留全部断言，仅用夹具显式启用；新测试验证默认清单与零 stdout。

验证：`uv run pytest dev/tests/test_bug06_debug.py dev/tests/test_tools.py -q（9 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-07

core/prompts.py 收敛提示词，core/agent.py 仅保留兼容导出并移除第二套循环；已有 CRUD 调度测试迁到 SessionManager，保留成功、拒绝、工具配对和文件内容断言。按本条要求修正 AGENTS.md 的测试路径；明确 AUDIT_LOG 仅无上下文时使用。无新配置。

验证：`uv run pytest dev/tests/test_bug07_hygiene.py dev/tests/test_agent_tools.py dev/tests/test_sessions.py -q（18 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-08

字符预算不能反映混合文本成本。core/context.py 增加 ASCII/非 ASCII token 估算、双预算与异步摘要；摘要限制输入输出和时限，仅修改工作副本，失败降级，按内容摘要缓存。core/sessions.py 接入并记录前后估算。新增 test_bug08_summary.py，fake judge 验证事实保留。新配置 MODEL_INPUT_TOKENS=16000、CONTEXT_SUMMARY=True、SUMMARY_INPUT_CHARS=12000、SUMMARY_CHARS=1000、SUMMARY_TIMEOUT=10。估算不是精确 tokenizer。

验证：`uv run pytest dev/tests/test_bug08_summary.py dev/tests/test_context.py dev/tests/test_sessions.py -q（22 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-09

大文件单次参数易截断。采用分段生成协议：tools/file_crud.py 增加 append_file（4000 字符、预期偏移、逐次确认、原子写与审计）；core/prompts.py 指导逐段落盘，core/context.py 压缩历史分段参数。测试实际保存 60k 中文字符并拒绝重复偏移、拒绝确认；原有不完整流不执行测试保持。无新增配置。

验证：`uv run pytest dev/tests/test_bug09_append.py dev/tests/test_file_crud.py dev/tests/test_llm_stream.py -q（36 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-10

保存时全量 deepcopy/序列化造成线性放大。core/storage.py 改为版本 2 消息 JSONL 增量追加及小型原子提交索引，旧格式备份后迁移，崩溃尾部不重放；异步保存只复制元数据并捕获不可变消息边界。core/cli.py 导出在线程读取已提交快照。SESSION_MAX_MB=64 控制新轮次准入，归档保留全部内容。test_bug10_storage.py 使用 10k 消息验证索引小于 5 KB、新消息写入小于 100 B及旧版备份/崩溃尾部恢复。

验证：`uv run pytest dev/tests/test_bug10_storage.py dev/tests/test_sessions.py dev/tests/test_cli.py dev/tests/test_cli_performance.py -q（26 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-11

工具循环串行且无调用总数边界。tools/base.py 增加 concurrency/timeout_s 元数据；core/sessions.py 有界并发连续只读批次、按原顺序回填结果、写操作串行、超额调用明确拒绝并配对，取消等待旧写线程。新增 MAX_TOOL_CALLS_PER_ROUND=8、TOOL_CONCURRENCY=4、TOOL_TIMEOUT=120。test_bug11_tools.py 验证慢读取并行和超额不执行；线程级取消边界见 CLI 文档。

验证：`uv run pytest dev/tests/test_bug11_tools.py dev/tests/test_sessions.py dev/tests/test_cli_performance.py -q（22 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-12

每次全读语料造成 IO 放大。rag/index.py 按文件指纹复用正文哈希，分块缓存，未变索引绕过重建锁；rag/lexical.py 缓存分词。新增 HTML 和可选 PDF/docx 加载器，pyproject.toml documents extra，uv lock/uv sync --locked 已同步（新增 lxml/pypdf/python-docx 仅可选）。test_bug12_index.py 验证 1000 文件第二次零读取、单改只读一文件及 HTML 去脚本。最小版本保持变化请求同步更新全局 BM25 IDF，不使用陈旧后台索引。

验证：`uv run pytest dev/tests/test_bug12_index.py dev/tests/test_hybrid_rag.py dev/tests/test_rag_search.py -q（31 passed）；uv sync --locked 成功`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-13

原始 logit 未校准且无质量门。新增 dev/rag/quality.py 与固定公开 qrels 清单：5 校准/5 留出、343 文档候选池，分开输入输出，文档去重指标、只用校准划分选阈值。真实链路已跑完，四阶段 Top3 和指标见 docs/rag-quality.md；默认阈值留出 hit@10/MRR 均 0.8，建议阈值未提高质量，默认保持不变。新增离线指标/划分测试与可选 integration 质量门；提示词要求引用编号作答。无新运行配置，测试入口 RAG_QUALITY_CORPUS。

验证：`uv run pytest dev/tests/test_bug13_quality.py -m "not integration" -q（3 passed, 1 deselected）；dev.rag.quality 真实 10 查询完成`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-14

记忆全量注入且无条目管理。新增 core/memory.py 兼容旧文本、结构化条目、BM25 top-k 和注入预算；core/storage.py 去重/容量/删除/显式共享，core/cli.py 增加 list/rm 与删除确认；core/sessions.py 可选后台候选提取，需用户 /remember 采纳，不自动改变有效记忆。新增 MEMORY_TOP_K=4、MEMORY_INJECT_CHARS=1200、MEMORY_AUTO_EXTRACT=False、MEMORY_SHARED=False。test_bug14_memory.py 覆盖去重删除和相关召回预算，旧隔离与记忆限制测试继续通过。

验证：`uv run pytest dev/tests/test_bug14_memory.py dev/tests/test_bug01_budget.py dev/tests/test_sessions.py dev/tests/test_cli.py -q（22 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-15

单一模型瞬时故障无备用。core/llm.py 支持同协议 MODEL_FALLBACKS，在流开始前重试失败/超时后切换，开始输出后不重放；独立客户端关闭与会话 ContextVar 路由指标。记录 provider usage 或含 schema 的近似 token 与配置价格成本（未知为 null）。新增 MODEL_FALLBACKS=[]、MODEL_STREAM_USAGE=False、INPUT_COST_PER_MILLION=0、OUTPUT_COST_PER_MILLION=0。test_bug15_fallback.py 注入主模型连接错误验证备用成功，并验证 provider usage/费用。

验证：`uv run pytest dev/tests/test_bug15_fallback.py dev/tests/test_llm_stream.py dev/tests/test_llm_retry.py dev/tests/test_bug05_config.py -q（20 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-16

评估将全部 query/context/answer 拼接，可能超窗。rag/assess.py 分配总预算、单片段上限，保留总命中数与纳入数说明，极小预算或 token 超限明确降级且不调用裁判。新增 ASSESS_INPUT_CHARS=12000、ASSESS_CONTEXT_CHARS=2000。test_bug16_assess.py 把超过 200 万字符的输入压到 2000 字符内并完成 fake judge。

验证：`uv run pytest dev/tests/test_bug16_assess.py dev/tests/test_assess.py dev/tests/test_sessions.py -q（20 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-17

低风险实现：core/sessions.py 保存最近 RUN_HISTORY_LIMIT=20 轮指标，分配 turn_id 并关联工具调用 ID；tools/sandbox.py 审计包含 session/turn/tool_call_id；JSON 结构化 turn_finished 诊断事件。不引入 OpenTelemetry 或成本面板。test_bug17_runs.py 验证三轮运行保留两轮及独立追踪 ID。

验证：`uv run pytest dev/tests/test_bug17_runs.py dev/tests/test_sessions.py dev/tests/test_cli_performance.py -q（22 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-18

按 P2 低风险范围实现：rag/cancellation.py 提供协作取消，rag/tool.py 阶段、embedding/rerank 批次间检查；工具超时独立取消标志；CLI 明确等待当前调用退出。已有 web 超时和独立评估池复用。不能强杀在途 native 线程，进程隔离方案与验收见 docs/platform-design.md，未承诺硬截止。test_bug18_cancel.py 验证停止后不进入昂贵索引。无新配置。

验证：`uv run pytest dev/tests/test_bug18_cancel.py dev/tests/test_hybrid_rag.py dev/tests/test_sessions.py dev/tests/test_command.py -q（60 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-19

按用户范围仅完成设计：docs/platform-design.md 明确 headless 参数、JSON/JSONL 契约、退出码、默认拒绝审批、配对和持久化验收；API/SSE 的状态锁、幂等/审批，MCP 权限与 schema 预算，多模态上传及网页出网边界。未实现新入口/监听服务、未新增配置/依赖；没有声称通过不存在的运行功能测试。

验证：`设计核对：CLI 默认入口、沙箱确认、会话锁与工具配对契约；无运行代码变更`。纯设计未新增运行功能测试。

## BUG-20

新增 .github/workflows/tests.yml：默认 PR/main push 锁定安装与离线测试，手动 integration 作业缓存公开模型/语料并跑真实链路和质量回归。dev/ci_sandbox_probe.py 实际验证 Bubblewrap/prlimit，不支持则报错退出，不跳过或退回宿主。test_bug20_ci.py 先复现缺工作流，再验证入口/命令/非静默失败契约。无新应用配置。远端 Actions 尚未触发。

验证：`uv run pytest dev/tests/test_bug20_ci.py -q（1 passed）；uv run python dev/ci_sandbox_probe.py（真实隔离通过）；uv run pytest -m "not integration" -q（185 passed，最终增补回归见交付报告）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## 新增配置项

| 名称 | 默认值 | 含义 |
|---|---|---|
| MEMORY_MAX_CHARS | 4000 | 持久记忆正文总上限；旧文件完整内容仍可导出 |
| ASSESS_CONCURRENCY | 1 | 独立后台评估池 |
| WEB_SEARCH_CONFIRM | always | 出网逐次确认；仅显式 off 关闭 |
| WEB_SEARCH_TIMEOUT | 15 | 搜索网络及检查总时限，秒 |
| WORKSPACE_LIMIT_MB | 512 | 命令工作区用量检查上限，MiB；非硬卷配额 |
| COMMAND_MEMORY_MB | 512 | 单进程地址空间上限，MiB |
| COMMAND_CPU_SECONDS | 5 | 单进程 CPU 秒数上限 |
| COMMAND_FILE_MB | 32 | 单文件尺寸上限，MiB |
| COMMAND_PROCESSES | 128 | rlimit 同 UID 进程数限制，root 等边界见沙箱文档 |
| ENABLE_DEBUG_TOOL | False | 显式向模型注册调试工具 |
| MODEL_INPUT_TOKENS | 16000 | 与字符预算同时生效的近似 token 上限 |
| CONTEXT_SUMMARY | True | 淘汰旧轮次时尝试有界摘要 |
| SUMMARY_INPUT_CHARS | 12000 | 摘要输入正文上限 |
| SUMMARY_CHARS | 1000 | 摘要输出上限 |
| SUMMARY_TIMEOUT | 10 | 摘要调用时限，秒 |
| SESSION_MAX_MB | 64 | 新轮次准入体积上限；在途结果不丢弃 |
| MAX_TOOL_CALLS_PER_ROUND | 8 | 每轮实际执行工具数，超额回填拒绝结果 |
| TOOL_CONCURRENCY | 4 | 只读工具线程槽位 |
| TOOL_TIMEOUT | 120 | 通用工具等待时限，秒；写线程必须收尾 |
| MEMORY_TOP_K | 4 | 相关记忆最大条数 |
| MEMORY_INJECT_CHARS | 1200 | 注入记忆字符上限 |
| MEMORY_AUTO_EXTRACT | False | 后台生成待采纳候选，不自动注入 |
| MEMORY_SHARED | False | 显式使用 shared-memory.md，默认保持会话隔离 |
| MODEL_FALLBACKS | [] | 同协议备用模型 JSON 数组，跨服务指定 api_key_env |
| MODEL_STREAM_USAGE | False | 请求服务商流式 usage；否则估算 |
| INPUT_COST_PER_MILLION | 0 | 输入每百万 token 美元价；未配成本为 null |
| OUTPUT_COST_PER_MILLION | 0 | 输出每百万 token 美元价；未配成本为 null |
| ASSESS_INPUT_CHARS | 12000 | 评估总输入字符预算 |
| ASSESS_CONTEXT_CHARS | 2000 | 评估单片段字符上限 |
| RUN_HISTORY_LIMIT | 20 | 最近完成轮次指标保留数 |

既有行为变化：配置模板 DEBUG 从 True 改为 False；MAX_CONCURRENT_AGENTS 现在只限制前台模型请求。
append_file 固定每段最多 4000 字符；记忆最多 64 条；存储 v2 增加 messages.jsonl，迁移保留 session.v1.bak。
RAG_QUALITY_CORPUS 是可选集成测试入口参数，默认不设置，不影响应用启动。

## 可验证最小版本与保留边界

- 沙箱增加资源兜底，但轮询不是硬磁盘配额，rlimit 是单进程/UID 语义；不宣称可安全托管不可信多租户。
- 存储采用增量日志和原子提交索引，完整对话仍加载到内存；体积上限控制新轮次准入，归档由用户显式导出，不自动清理。
- RAG 复用未变文件/分块/分词，但变化请求仍同步更新全局 BM25 IDF 和矩阵；没有后台陈旧索引。文件事件不可用时保守重读。
- 小候选池质量报告不能推导全库准确率。备用模型只在流开始前切换；成本为提供的 usage/估算和配置价格，不是账单。
- 在途原生推理线程无法强制安全终止，BUG-18 不承诺硬截止；API、MCP、多模态与 headless 的运行实现按本轮要求保留设计。
