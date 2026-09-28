# 使用自由度与硬编码限制（FREE）

> 所有 `file:line` 为基线 `7e7499b`。开放能力必须默认不改变现有行为与安全边界。

---

## FREE-01 工具根目录只有单一沙箱

**现状**：`tools/sandbox.py:79-99` 只允许 `SANDBOX_DIR/<session>` 内的相对路径；无法让 Agent 读取项目代码/文档或用户指定的其他目录；没有只读挂载概念。

**开放方向**
1. `TOOL_ROOTS` 配置：命名根（如 `workspace`、`docs:ro`、`project:ro`），工具参数可用 `docs/xxx` 前缀。
2. 只读根禁止写工具；越界检查与现有 `resolve_path` 共用同一套规范化逻辑。
3. 默认仅 workspace，行为不变。

**验收**：新增只读根后 `read_file/list_files/rag` 可访问，写工具仍拦截；越界与 symlink 测试全部保留。

---

## FREE-02 命令沙箱固定：10 秒、断网、无包管理、仅 Linux

**现状**：`config.py:77` `COMMAND_TIMEOUT=10`；`tools/sandbox.py:207-219` `--unshare-all` 固定断网；隔离内无项目 venv、无法 `pip install`；依赖 bwrap + prlimit，非 Linux 不可用；单会话命令串行、无长任务模式。

**开放方向**
1. 可选网络模式：`COMMAND_NETWORK=off|allowlist`，配域名白名单代理并在每次联网命令前确认与审计。
2. 长任务模式：后台执行 + 日志文件 + `job_status/job_logs` 读取工具（仍受配额与超时约束）。
3. 可选只读挂载项目目录/venv，用于本地计算。
4. 非 Linux：明确错误 + Docker/Podman 后端设计说明。

**验收**：默认断网与 10s 不变；新档位显式配置、有确认、有审计、有超时与配额测试。

---

## FREE-03 文件工具能力缺口

**现状**：`tools/file_crud.py` 仅 create/read/edit/update/delete/append/list；无 `mkdir`、`move`、`copy`、递归列表、通配、正则查找、编码选择、文件大小上限查询；`delete_file` 只删文件不支持目录；`list_files` 不递归（:268-298）。

**开放方向**：补齐目录与批量工具（mkdir/move/copy/stat/glob），`read_file` 支持正则与行号，编码可配；全部沿用确认、原子写、配额与审计。

**验收**：常见整理任务无需 `run_command` 即可完成；路径边界、配额、确认测试齐全。

---

## FREE-04 权限模型只有逐次确认或全局关闭

**现状**：写/命令每次人工确认（`tools/sandbox.py:174-186`）；`WEB_SEARCH_CONFIRM` 仅 `always|off`（`tools/web_search.py:26`）；没有「只读放行」「命令前缀白名单」「本轮全部批准」「某路径已信任」等档位。

**开放方向**
1. 权限档位 `readonly|standard|trusted` + 规则：按工具、路径前缀、命令前缀、单轮批准。
2. CLI 增加 `/approve all`、`/policy` 查看当前策略；审计记录「因何策略放行」。
3. 默认 standard（现状），拒绝仍 fail-closed。

**验收**：默认行为逐字不变；每个档位有测试；越权路径/命令仍被拦截并审计。

---

## FREE-05 模型参数与工具选择硬编码

**现状**：`temperature/top_p/max_tokens/parallel_tool_calls/response_format` 均不可配；`core/llm.py:156` `tool_choice='auto'` 硬编码；后台 `complete()` 固定主模型（:291-312）；备用模型要求同协议（:141-166）；无按任务选模型。

**开放方向**：`MODEL_PROFILES`（采样参数、模型、用途）+ 会话级覆盖；`tool_choice` 模式；`AUX_MODEL` 供摘要/评估/记忆；provider 适配层（聊天气泡外的协议差异）。

**验收**：不配置时行为与现在一致；新增 profile 有测试与文档。

---

## FREE-06 预算与轮次全局单值

**现状**：`MAX_TOOL_ROUNDS=24`、`MAX_TOOL_CALLS_PER_ROUND=8`、`MODEL_INPUT_TOKENS=16000`、`MODEL_OUTPUT_CHARS=48000`、恢复仅一次、`TOOL_TIMEOUT=120` 均为全局配置，不能按会话/任务覆盖。

**开放方向**：会话级预算（提交时参数或 `/config` 设置），随会话持久化；提供保守/标准/激进预设；恢复次数可配。

**验收**：默认不变；覆盖值写入 `session.json` 并在恢复后生效；UI 显示剩余预算。

---

## FREE-07 RAG 数据源与查询能力受限

**现状**：单一 `DOC_DIR`（`config.py:58`）；固定扩展名集合（`rag/index.py:83`）；embedding/reranker 模型全局固定；无 metadata 过滤；`breadth` 只映射 {2,4,8}（`rag/tool.py:10`）且工具层不暴露 `top_k`；无 URL/数据库接入；无查询改写/多查询/HyDE。

**开放方向**
1. 多知识库：命名 source + `source` 过滤参数；每库可独立模型/阈值。
2. 工具参数增加 `top_k`、`source`、`updated_after`。
3. URL/目录导入命令（写入 `data/raw` 或新建库）。
4. 查询改写/多查询开关（默认关）。

**验收**：默认单库输出不变；多库与过滤有测试；质量报告沿用 `dev.rag.quality`。

---

## FREE-08 记忆能力受限

**现状**：自动提取默认关闭（`MEMORY_AUTO_EXTRACT=False`，`core/sessions.py:327`），候选需人工采纳；无标签/来源过滤、无过期与时间衰减；`MEMORY_SHARED` 只能在「每会话」与「单一共享文件」间二选一（`core/storage.py:212-214`）；无 `/memory search`。

**开放方向**：记忆命名空间（global/project/session）、标签与来源过滤、过期时间、时间衰减排序、自动采纳可选、`/memory search|edit`。

**验收**：默认隔离、上限、人工采纳语义不变；新增管理命令有测试与文档。

---

## FREE-09 接口单一（TUI）且单进程锁

**现状**：只有全屏 CLI；`core/storage.py:40-45` flock 限制同一状态目录只能一个进程；无 headless/API/SDK/Web；无文件上传下载；跨进程无法共享会话。

**开放方向**
1. P1：`--prompt/--json` headless 单次执行（复用 `SessionManager`，非交互不确认时默认拒绝写操作）。
2. P2：HTTP API + SSE 流式（按 `docs/platform-design.md` 契约），鉴权、并发与审批；存储锁升级为多客户端方案。
3. 文件上传/下载接口与工作区浏览。

**验收**：CLI 行为不变；headless/API 有独立测试与安全默认（写操作默认拒绝）。

---

## FREE-10 会话不可分支、不可编辑重发

**现状**：无 `/branch`、无编辑历史消息、无重新生成；项目最初定位提到「易于上下文分支」但未实现；工作区与会话一一对应，复制会话需要手工操作。

**开放方向**：从任意消息 fork 会话（消息前缀复制，工作区可选复制/引用）；编辑并重发；重新生成最后一轮；分支关系可在侧栏展示。

**验收**：原会话与工作区不受影响；新旧格式兼容；有 fork/编辑的并发与存储测试。

---

## FREE-11 无后台任务、定时与子代理

**现状**：每个会话同时只跑一项任务；无任务队列、定时触发器、webhook；无子代理（隔离上下文 + 预算 + 结果回注）。

**开放方向**：持久任务队列 + 定时/触发；`delegate` 子代理工具（预算与作用域显式）；主会话可异步收取结果。

**验收**：并发上限可配；子代理继承沙箱/确认/配额；失败可观测。

---

## FREE-12 多模态缺失

**现状**：仅文本输入；无图片/音频/视频；PDF/DOCX 仅服务于 RAG 索引（`rag/index.py:47-58`）。

**开放方向**：图片输入（provider 支持视觉时）、截图回传、附件上传；能力探测后对不支持模型明确降级提示。

**验收**：默认文本行为不变；不支持时提示而非报错。

---

## FREE-13 成本与速率无预算、无熔断

**现状**：`core/llm.py:269-278` 已记录 token/成本，但不设上限、不告警、不熔断；RAG/重排也不计入成本。

**开放方向**：`SESSION_COST_LIMIT`、`DAILY_COST_LIMIT`、请求速率限制；超限暂停并提示；成本面板/`/usage`。

**验收**：超限行为可测；默认不限；统计与账单口径在文档中说明。

---

## FREE-14 可观测只落在文件

**现状**：指标写入 `session.json` 与 `cli.log`；`RUN_HISTORY_LIMIT=20`；无查询命令、无 trace 查看、无用户反馈入口；`docs/platform-design.md` 只有设计。

**开放方向**：`/usage`、`/trace` 查看最近轮次；JSONL trace（已有设计）；用户对回答的反馈记录（可选）。

**验收**：不影响现有日志格式；大字段脱敏。

---

## FREE-15 跨平台受限

**现状**：命令工具硬依赖 Linux bwrap/prlimit；TUI 需要交互终端；无 headless，WSL/macOS/Windows 只能使用文件工具。

**开放方向**：headless 模式（P1）解除 TTY 依赖；容器后端（Docker/Podman）或平台专用沙箱作为可选实现；文档明确各平台能力矩阵。

**验收**：非 Linux 给出可操作提示与替代路径；不允许静默降级为主机执行。

---

## FREE-16 提示词与工具描述硬编码数值

**现状**：`core/prompts.py:4-8` 写死「4000 字符/每次最多 2 次检索重试」；`tools/file_crud.py:301-305` `append_file` 4000 上限写进 schema；工具重试次数、分页提示等与配置解耦；系统提示不可替换。

**开放方向**：提示词模板化，从配置注入数值（分段上限、重试次数、分页大小）；支持用户自定义系统提示文件（`/prompt` 已有，但仅当前会话）；默认文本保持等价。

**验收**：改配置后提示词与 schema 描述同步变化；默认行为不变；有快照/等价性测试。

## 实施追加记录

### FREE-03（完成）

显式 ENABLE_FILE_EXTRAS=false 注册开关；补充五种工具，写入确认/配额/无覆盖原子发布，glob 不进入链接目录。move/copy 当前仅支持普通文件。30 项文件回归通过。详见 docs/optimization-progress.md。

### FREE-05（完成）

显式 MODEL_PARAMETERS/MODEL_PROFILES，默认请求等价；/model 持久化并发隔离预设。31 项模型/会话回归通过，未调用收费服务。详见 docs/optimization-progress.md。

### FREE-13（完成）

显式费用/速率额度默认 0 不限；原子账本预留、provider usage 结算、未知单价拒绝、超限检查点；27 项回归通过。估算不是账单硬上限。详见 docs/optimization-progress.md。

### FREE-16（完成）

提示词/参数 schema 共用配置，显式系统提示文件，默认文本快照完全一致。完整离线回归 222 passed、3 deselected；随后快照专项 3 passed。详见 docs/optimization-progress.md。

### FREE-01（完成：命名只读根）

TOOL_ROOTS 默认空；显式 @名称/路径用于 read/list/stat/glob，rag_search(source='@名称') 使用受边界限制的索引。
全部写工具拒绝只读根；从只读根复制到工作区仍需确认；不挂载到命令沙箱。文件/命令专项 63 passed，真实 bwrap 探针与真实命名根 RAG 测试通过。
完整回归 258 passed，2 项可选 ANN 跳过、4 项集成未启用。配置与实现范围见 docs/optimization-progress.md。
