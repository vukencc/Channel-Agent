# P0 阻断问题（优先修复）

> 基线提交：`93a7fed`。严重级别与工作流见 [README.md](README.md)。
> 每个 BUG：先补失败测试，再实现；单独提交；不得放宽沙箱、确认、审计与工具配对语义。

---

## BUG-01 上下文预算计量缺口与硬失败

**级别** P0　**状态** 已修复（说明见下）　**文件** `core/context.py`、`core/llm.py`、`core/storage.py`、`core/sessions.py`

**症状**
- 长历史、大记忆或多工具参数场景下，会话可能直接进入 `error`。
- 实际请求可能已超过模型上下文窗口，但本地预算计数没有察觉。

**证据**
- `core/context.py:52-64`：压缩后仍超 `MODEL_INPUT_CHARS` 时 `raise ValueError`，`core/sessions.py:302-304` 捕获后状态置 `error`。
- `core/context.py:8-9`：`history_size` 只序列化消息；每轮随请求发送的工具 schema（`core/llm.py:46,119`）未计入预算。
- `core/storage.py:161-164`：`/remember` 无上限追加；`core/sessions.py:225-231` 每轮把整份记忆拼进 system。

**修复建议**
1. 预算计算包含工具 schema 与每轮追加文本（`FILE_WORKFLOW_GUIDE`、剩余轮数提示、记忆），`last_run.context` 输出分解：messages / schema / memory / 追加文本。
2. 超限不再抛异常：保存为 `checkpoint`（与 `MAX_TOOL_ROUNDS` 用尽一致），提示具体超限项与可操作建议，完整记录不删。
3. `/remember` 增加 `MEMORY_MAX_CHARS` 上限（建议默认 4000），超限拒绝并提示；读取时超限截断并标注。
4. 测试覆盖：超预算会话保持可读、状态为 checkpoint、记忆超限被拒、schema 计入预算。

**验收**
- 100+ 轮、大记忆、10 个工具场景下不再因预算抛异常。
- 超限时用户可「继续」或新建会话，完整历史可导出。
- `uv run pytest -m 'not integration' -q` 全绿。

---

**修复说明** 根因是预算遗漏 schema 与无界记忆。core/context.py 统计 messages/schema/memory/extra；core/sessions.py 将预算不足转换为 checkpoint；core/storage.py 限制记忆追加与注入。新增 test_bug01_budget.py 三项回归，既有慢上下文测试仅适配参数签名，断言保持。配置 MEMORY_MAX_CHARS=4000。 验证：`uv run pytest dev/tests/test_bug01_budget.py dev/tests/test_context.py dev/tests/test_sessions.py -q（23 passed）`。

## BUG-02 并发信号量被人工确认与后台评估占用

**级别** P0　**状态** 已修复（说明见下）　**文件** `core/sessions.py`、`config.py`

**症状**
- 4 个会话等待用户确认时，其他会话全部排队，无法启动。
- RAG 后台评估与正常对话抢同一个并发池，可互相阻塞。
- `MAX_CONCURRENT_AGENTS` 的实际语义与文档不一致。

**证据**
- `core/sessions.py:51` 定义 `self.slots`；`:213` `_run` 用 `async with self.slots` 包住整轮（含 `_tool` 等待确认与工具执行）。
- `core/sessions.py:197` 后台评估同样 `async with self.slots`。

**修复建议**
1. 信号量只包模型调用，或在确认等待期间释放槽位；保证同一会话仍串行。
2. 评估使用独立低优先级池（如 `ASSESS_CONCURRENCY=1`），不与对话共享 `slots`。
3. 明确并文档化 `MAX_CONCURRENT_AGENTS` 与模型调用并发的关系，更新 `.env.example` 与 `docs/cli.md`。

**验收**
- 并发测试：4 个会话处于待确认时，第 5 个会话仍能调用模型并完成。
- 后台评估运行期间新任务不被阻塞。
- `dev/tests/test_sessions.py` 全过。

---

**修复说明** 整轮持有信号量造成确认阻塞；core/sessions.py 改为仅模型调用占槽，评估独立池 ASSESS_CONCURRENCY=1。新增 test_bug02_slots.py 验证四个等待工具时第五会话可完成。 验证：`uv run pytest dev/tests/test_bug02_slots.py dev/tests/test_sessions.py -q（15 passed）`。

## BUG-03 web_search 免确认出网、无超时/上限/截断

**级别** P0　**状态** 已修复（说明见下）　**文件** `tools/web_search.py`、`tools/sandbox.py`、`.env.example`

**症状**
- 提示词注入可驱动模型把会话或文件内容拼进搜索 query 发往 Tavily（数据外泄面）。
- 网络慢时占用工作线程且无法取消；超大结果直接进入上下文。
- 缺少 API Key 时客户端进入 keyless 模式，行为不明确。

**证据**
- `tools/web_search.py:9` 导入时创建客户端；`:29-33` 直接调用，无 timeout、无 `max_results` 上限、无 `truncate`、异常未包装。
- 现有确认机制只覆盖写文件与命令，不覆盖网络工具（`tools/sandbox.py:155-167`）。

**修复建议**
1. 增加 `WEB_SEARCH_TIMEOUT`、`max_results` 上限（建议 10）、结果 `truncate()`、异常包装为可读工具结果。
2. 增加网络工具确认策略，例如 `WEB_SEARCH_CONFIRM=always|first|off`，默认 `always` 或至少新会话首次确认；确认面板显示完整 query。
3. 审计记录网络查询（事件类型 `web_search`）。
4. 缺少 `WEB_SEARCH_API_KEY` 时给出明确提示，不静默使用 keyless。

**验收**
- 注入慢响应/大结果测试；拒绝确认时不发出请求。
- 新配置写入 `.env.example` 与 `docs/`。
- 现有测试不回归。

---

**修复说明** tools/web_search.py 移除导入时客户端，改用有超时及 2 MB 响应上限的 HTTP 流；默认逐次出网确认，查询完整展示、审计，缺 key 拒绝。参数限制 1–10 条、输出截断。新增 WEB_SEARCH_CONFIRM=always（可 off）、WEB_SEARCH_TIMEOUT=15 秒。测试覆盖拒绝不请求、参数越界、慢响应与大结果。 验证：`uv run pytest dev/tests/test_bug03_web.py dev/tests/test_tools.py -q（10 passed）`。

## BUG-04 沙箱无 CPU/内存/磁盘/进程数配额

**级别** P0　**状态** 已修复（说明见下）　**文件** `tools/sandbox.py`、`tools/command.py`

**症状**
- 单条 10 秒命令即可写满磁盘或创建大量进程，拖垮主机。
- 文档已声明「不是资源隔离服务」，但缺少任何兜底限制。

**证据**
- `tools/sandbox.py:179-201`：仅 `--unshare-all`、`--cap-drop ALL`、只读挂载，无 `--rlimit-*`、无 seccomp、无磁盘配额。
- `tools/command.py:71` 只限制捕获输出大小，不限制子进程写入磁盘的量。

**修复建议**
1. 使用 bwrap 支持的 `--rlimit-*`（as/cpu/fsize/nproc，按可用版本探测并记录），或等价的宿主侧限制。
2. 执行前后检查工作区磁盘用量，新增 `WORKSPACE_LIMIT_MB`（建议默认 512），超限拒绝执行并审计。
3. `docs/sandbox.md` 的边界说明与实现保持一致；无 bwrap 环境给出明确错误，不回退主机执行。

**验收**
- fork 炸弹/大文件写入用例被限制或拒绝；正常命令与 CRUD 测试全过。
- 新增测试覆盖磁盘配额与 rlimit 探测失败路径。

---

**修复说明** 缺少资源限制。tools/sandbox.py 探测宿主 rlimit 能力与 prlimit，命名空间建立后设置 AS/CPU/FSIZE/NPROC；tools/command.py 执行前中后检查工作区大小，超额终止并审计，不删除文件。新增五项配置见 .env.example；轮询非硬磁盘配额、按进程/UID 限制边界详见 docs/sandbox.md。新增 test_bug04_limits.py 验证超额拒绝、缺启动器失败关闭、实际大文件受限。 验证：`uv run pytest dev/tests/test_bug04_limits.py dev/tests/test_command.py dev/tests/test_file_crud.py -q（48 passed）`。

## BUG-05 配置缺失或非法在启动时不报错

**级别** P0　**状态** 已修复（说明见下）　**文件** `config.py`、`core/cli.py`

**症状**
- 缺少 `OPENCODE_API_KEY`、`BASE_URL`、`MODEL` 时，程序照常启动，直到首次模型调用才以会话 `error` 形式暴露。
- `env_int`/`env_float` 解析失败抛裸 `ValueError` 栈，对用户不友好。

**证据**
- `config.py:37-39`：三个关键值均可能为 `None`。
- `config.py:15-25`：解析函数无容错与错误上下文。
- `core/llm.py:30-40`：客户端在首次调用时才创建。

**修复建议**
1. 新增 `validate_runtime_config()`，在 `core/cli.py:main()` 启动时调用：校验模型三要素、`DOC_DIR`、`SANDBOX_DIR`、数值范围，给出中文可操作错误与退出码。
2. `env_int/env_float` 解析失败时抛出带变量名的可读错误。
3. 测试：缺失/非法配置启动失败且信息明确；合法配置不产生额外输出。

**验收**
- `uv run python main.py` 在缺配置时给出明确提示并非零退出。
- `dev/tests/test_config.py` 扩展通过。

---

**修复说明** config.py 增加集中启动校验并让数字解析错误携带变量名；core/cli.py 在创建状态目录前验证，main.py 捕获导入期配置错误并以退出码 2 输出中文提示。--list 无需模型密钥。新增 test_bug05_config.py 覆盖缺 key、非法范围、正确配置静默。无新配置。 验证：`uv run pytest dev/tests/test_bug05_config.py dev/tests/test_config.py dev/tests/test_cli.py -q（见提交验证）`。

## BUG-06 调试工具常驻生产、DEBUG 输出污染 TUI

**级别** P0　**状态** 已修复（说明见下）　**文件** `tools/__init__.py`、`tools/debug.py`、`tools/rag_search.py`

**症状**
- `tool_debug` 每次请求都发送 schema，浪费上下文并可能被模型误选。
- `.env.example` 默认 `DEBUG=True` 时，`rag_search` 把完整结果 print 到 stdout，全屏 CLI 下可能串屏。

**证据**
- `tools/__init__.py:8` 导入 `debug` 即注册；`tools/debug.py:18` 注册 `tool_debug`。
- `tools/rag_search.py:41-42`：`if config.DEBUG: print(result)`。
- `.env.example:20`：`DEBUG=True`。

**修复建议**
1. `tool_debug` 仅在显式开启（如 `ENABLE_DEBUG_TOOL=1` 或 `DEBUG`）时注册，生产默认不注册。
2. `rag_search` 的 print 改为 `logger.debug`（避免任何工具向 stdout 直接输出）。
3. 更新 `dev/tests/test_tools.py` 等对工具清单的期望。

**验收**
- 默认启动时模型可见工具不含 `tool_debug`。
- 全屏 CLI 下工具执行不向 stdout 输出内容。

---

**修复说明** 调试工具无条件注册与 stdout print 导致 schema 浪费及串屏。tools/__init__.py 显式 ENABLE_DEBUG_TOOL=True 才注册；tools/rag_search.py 改为日志长度指标。默认 DEBUG=False、ENABLE_DEBUG_TOOL=False。既有调试测试保留全部断言，仅用夹具显式启用；新测试验证默认清单与零 stdout。 验证：`uv run pytest dev/tests/test_bug06_debug.py dev/tests/test_tools.py -q（9 passed）`。

## BUG-07 工程卫生：死代码、文档路径、AUDIT_LOG 语义

**级别** P0　**状态** 已修复（说明见下）　**文件** `core/agent.py`、`core/sessions.py`、`AGENTS.md`、`config.py`

**症状**
- 存在两套 Agent 循环：`core/agent.py:50-105` 的旧 `input()/print()` 循环不可达，但 `core/sessions.py:11` 仍从该模块导入常量，增加维护与误用风险。
- `AGENTS.md` 写测试在 `tests/`，实际 `pyproject.toml:33` 为 `dev/tests`。
- `AUDIT_LOG`（`config.py:71`）在 CLI 路径下不会生效，实际写入每会话 `audit.jsonl`。

**证据**
- `core/agent.py:50-105`（`session_loop`/`create_session`）、`core/agent.py:41-47`（`assess_turn`）均无生产调用路径。
- `AGENTS.md`「Testing Guidelines」与 `pyproject.toml` 不一致。
- `tools/sandbox.py:95-99`：有 `ToolContext` 时优先 `context.audit_path`。

**修复建议**
1. 将 `DEFAULT_PROMPT`、`FILE_WORKFLOW_GUIDE` 迁移到 `core/prompts.py`（或保留 `core/agent.py` 仅作兼容导出并标注废弃），删除不可达循环与 `create_session/assess_turn`。
2. 修正 `AGENTS.md` 的测试路径与命令示例。
3. 明确 `AUDIT_LOG` 的语义：仅无上下文时使用，或在文档说明 CLI 使用每会话审计文件；必要时为 CLI 提供全局审计开关。
4. 检查是否仍有其他模块导入 `core.agent` 的死代码接口。

**验收**
- 全仓 `rg 'session_loop|create_session|assess_turn'` 无生产引用。
- 文档与 `pyproject.toml` 一致。
- 现有测试全绿。

**修复说明** core/prompts.py 收敛提示词，core/agent.py 仅保留兼容导出并移除第二套循环；已有 CRUD 调度测试迁到 SessionManager，保留成功、拒绝、工具配对和文件内容断言。按本条要求修正 AGENTS.md 的测试路径；明确 AUDIT_LOG 仅无上下文时使用。无新配置。 验证：`uv run pytest dev/tests/test_bug07_hygiene.py dev/tests/test_agent_tools.py dev/tests/test_sessions.py -q（18 passed）`。
