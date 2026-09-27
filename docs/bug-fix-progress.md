# 缺陷修复清单与验证记录

每项先失败测试，再最小修复和独立提交；所有运行数据使用临时目录。

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
- [ ] BUG-14
- [ ] BUG-15
- [ ] BUG-16
- [ ] BUG-17（P2：低风险实现或设计）
- [ ] BUG-18（P2：低风险实现或设计）
- [ ] BUG-19（P2：低风险实现或设计）
- [ ] BUG-20（P2：低风险实现或设计）

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

验证：`uv run pytest dev/tests/test_bug05_config.py dev/tests/test_config.py dev/tests/test_cli.py -q（见提交验证）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

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
