# 多会话 Agent CLI

```bash
uv sync --locked
uv run ai-agent-startup
uv run ai-agent-startup --state-dir /path/to/private-state
uv run ai-agent-startup --list
```

需要交互终端；隔离命令仍要求 Linux + Bubblewrap。API、模型、RAG 与工具配置沿用 `.env`。

## 布局与操作

左侧显示会话名称、ID、运行状态；右侧显示历史、流式回复及工具结果；底部是多行输入和阶段状态。
点击左侧或 Ctrl+左右方向键切换，会话中的任务继续运行。PageUp/PageDown 翻阅完整历史，Ctrl+Home 跳到最早内容，Ctrl+End 恢复跟随最新。
F2 或 `/sidebar` 可隐藏会话侧栏，扩大正文区域；Tab 在输入区与对话区切换。
Enter 发送，Alt+Enter 换行；Ctrl+N 新建，Ctrl+C 停止当前任务，Ctrl+Q 保存并退出。

| 命令 | 用途 |
|---|---|
| `/new 名称` | 创建新 Agent 会话 |
| `/tools` | 查看当前会话可用工具子集；可用 `/tools all`、`/tools none` 或 `/tools 名称,...` 调整 |
| `/tasks` | 查看本会话创建的子任务；`/tasks cancel ID` 取消任务（详见[会话工具](session-tools.md)） |
| `/plan` | 只读查看当前会话的任务计划、步骤状态与阻塞原因（详见[任务计划使用说明](task-plans.md)） |
| `/switch ID前缀` | 恢复显示或切换已加载会话 |
| `/sessions`、`/resume` | 搜索并恢复会话（包含已隐藏会话） |
| `/models` | 搜索并选择模型预设 |
| `/permissions` | 搜索并选择权限模式 |
| `/policy 模式` | 设置当前会话权限模式；`default` 恢复环境默认 |
| `/delete [ID前缀]`、`/del` | 明确确认后将该会话及其委派后代移入回收区 |
| `/cleanup [cache|logs|sessions|all]` | 预览所选内容并明确确认清理；无参数只显示预览 |
| `/details [on|off]` | 显示或隐藏工具参数、结果与思考详情；只改变显示 |
| `/rename 名称` | 重命名当前会话 |
| `/prompt 指令` | 空闲时设置当前 Agent 的系统指令 |
| `/remember 内容` | 写入当前会话的长期记忆 |
| `/memory` | 查看记忆及其文件位置 |
| `/forget` | 确认后清空记忆，不删除对话 |
| `/export md`、`/export json` | 导出对话、工具调用和记忆 |
| `/yes`、`/no` | 处理当前会话的确认（Ctrl+Y / Ctrl+R） |
| `/stop` | 停止当前任务，保留已执行操作与结果 |
| `/close` | 隐藏空闲会话，保留持久文件 |
| `/older`、`/newer` | 上一页 / 下一页历史 |
| `/top`、`/bottom` | 最早历史 / 跟随最新 |
| `/where` | 查看实际文件工作区与会话目录 |
| `/sidebar` | 隐藏 / 显示侧栏 |
| `/help`、`/quit` | 帮助 / 保存并退出 |

普通消息若需要以 `/` 开头，输入 `//`。每个会话同时执行一项任务；同一会话运行中不接受第二条消息，
可创建其他会话并行工作。模型调用并发执行，阻塞工具在线程池中执行，默认最多 4 个前台模型请求，
其余模型请求排队；确认和工具执行不占模型槽位；`MAX_CONCURRENT_AGENTS` 和 `MAX_TOOL_ROUNDS` 控制并发数和工具轮数。
共享 RAG 模型/索引可能等待资源锁；并发并不保证 CPU 推理加速。

输入 `/` 可补全命令并查看参数提示；Tab 接受补全。Ctrl+P 打开可搜索命令面板；`/sessions`、`/resume`、`/models`、`/permissions` 打开对应的搜索选择器，使用上下键和 Enter 选择，Esc 返回。参数命令会填入输入框供编辑。

权限模式与会话删除见[CLI 命令与权限模式](cli-command-modes.md)。会话数据默认位于 `.agent/<会话ID>/`；删除会把该会话及委派子 Agent 的会话目录移入状态目录的 `.trash/`（默认 `.agent/.trash/`），独立手动分支保留。任一关联 Agent、工具或长命令仍在运行或完成审计时，删除会被拒绝。回收目录可在退出 CLI 后完整移回状态目录并恢复原 ID；不要与已有会话目录手工合并。删除不会移除工作区、导出文件或共享记忆。

`/cleanup` 不带参数时只预览缓存、日志和会话记录；指定 `cache`、`logs`、`sessions` 或 `all` 后会再次显示预览，并等待当前会话通过 `/yes` 明确确认（`/no` 取消）。确认绑定到发起操作的会话。缓存或日志的预览错误分别显示；其他类别仍可单独预览和选择。Agent 或工具仍在运行时会拒绝清理。

项目内缓存只能位于 `PROJECT_ROOT/.cache/`；项目外配置的专用 RAG 缓存也可清理，但不能与工作区、知识库原文、状态目录或显式本地模型目录重叠。缓存包含下载的模型，清理后下次检索可能需要重新下载，并会按需重置和加载运行时缓存。

日志项清空 Agent 主审计、会话级审计与生命周期日志、`cli.log` 和已知命令日志，同时保留新的清理审计记录；会话项把所有当前会话及委派子 Agent 的记录移入状态目录的 `.trash/`（默认 `.agent/.trash/`），并为 CLI 打开一个空白会话。工作区文件、知识库原文、共享记忆和导出文件均保留。

CLI 默认隐藏工具参数、工具结果及模型思考详情，只展示用户消息和 Agent 回复。`/details on` 显示这些执行细节，`/details off` 隐藏；该切换只影响界面，完整历史和导出不变。

启动时没有已恢复会话、执行 `/new`，或 headless 新建会话时，入口会调用已注册的 `create_session` 工具一次来创建空会话，不会为此额外请求模型。模型也可调用 `create_session` 创建子 Agent；Standard、Trusted、Smart 模式要求用户确认，Full Access 会自动批准工具确认，readonly 会拒绝。每个根会话有独立项目工作区；新建子 Agent 自动使用直接父 Agent 的项目工作区，并继承完整权限策略。子 Agent 省略 `tool_names` 时继承父会话完整工具集；显式指定时只能收窄。子 Agent 不能继续创建子 Agent。通信工具和完整行为限制见[会话工具](session-tools.md)。

项目文件在根会话之间隔离；子 Agent 自动共享直接父 Agent 的项目工作区。会话上下文、记忆和审计仍各自独立。已持久化的旧隔离子会话不会迁移文件；`AGENT_WORKSPACE_MODE` 旧环境变量不再生效。详见[会话工具](session-tools.md)。

`/plan` 只查看当前会话的 LLM 任务计划，不修改计划，也不触发工具。计划工具和配置默认关闭；启用后的工具用法、状态语义与权限边界见[任务计划使用说明](task-plans.md)。

## 文件持久化

默认位置是项目的 `.agent/`，已加入 Git 忽略规则；这是长期用户数据，不是可随意清理的缓存。

```text
.agent/
  <会话ID>/
    session.json   系统指令、消息提交索引、任务状态与时间
    messages.jsonl 完整消息与工具结果（v2 增量日志）
    memory.md      用户保存的长期记忆；每轮模型请求时重新读取
    audit.jsonl   当前 Agent 的工具审计
    inbox.json    父子 Agent 通信信箱（首次收信后创建）
  exports/        导出的 Markdown / JSON 文件
  cli.log         CLI 诊断日志
crud_tests/
  <会话ID>/       当前 Agent 的 CRUD / 命令输出（SANDBOX_DIR 可配置）
```

每个关键步骤由专用写线程按顺序原子保存，重启自动加载历史与记忆；JSON 编码和 fsync 不在 UI 线程执行。退出会等待待保存记录写完。意外终止的任务标记为中断，不自动重放工具，
因为文件操作可能已经成功；先检查工作区，再决定是否继续。流式输出正常结束或取消时保存，
进程被强制杀死时尚未保存的流式尾部可能丢失。导出使用唯一文件名，不覆盖之前的导出。

每个根会话绑定自己的项目工作区 `SANDBOX_DIR/<根会话ID>/`；子 Agent 使用直接父 Agent 绑定的项目目录。默认根目录位于项目的 `crud_tests/`。知识库和模型配置仍共享。
状态目录只保存记录、记忆和审计，不作为工具输出目录。即使使用 `--state-dir` 指向缓存位置，工具输出仍遵循 `SANDBOX_DIR`。
旧版本 `<状态目录>/<会话ID>/workspace/` 中的文件会在首次执行工具时复制到新位置，原文件保留；
若新旧工作区同时存在且没有完成迁移标记，会提示冲突，不覆盖文件。退出旧版 CLI 后再启动新版，使用 `/where` 核对路径。
记忆通过显式 `/remember` 保存，不会未经确认跨会话自动提取或共享。

同一持久化目录只允许一个 CLI 进程写入，以防覆盖；需要第二个 CLI 时使用不同的 `--state-dir`。
自定义状态目录应位于个人可控的位置，并自行排除版本控制。会话文件包含用户输入与工具结果。

## 确认与取消

确认面板绑定当前会话，其他会话的确认在左侧标为“待确认”。切换后再批准，不会把输入错发给另一个 Agent。
拒绝、超时、退出都按取消处理。停止隔离命令会终止进程；不能强制杀死 Python 工具线程，
已进入执行的 RAG/网络工具可能要等当前调用结束，该会话显示停止中，其他会话仍可操作。
停止不会撤销已经完成的文件写入，也不会自动重试副作用工具。

RAG 自动评分在后台执行，回答完成后可立即继续同一会话；新一轮会取消尚未完成的旧评估，防止结果串轮。
评估最多等待 `ASSESS_TIMEOUT` 秒，失败不影响回答，退出时取消。

## 验证记录

CLI/session 工具本轮实现、验证和合成界面基准见 [交付报告](cli-session-delivery-2026-09-29.md)；真实模型诊断见 [系统诊断报告](system-diagnostics-2026-09-27.md)。

## 本地交互性能与显示

流式刷新合并为约 12 次/秒，缓存已完成消息；后台会话不会重复重建当前正文。
启动时历史记录采用惰性加载，长历史的页面整理在后台线程完成；切换会话时保留各会话输入草稿，状态与通知刷新会合并处理。这些机制减少终端界面等待，不代表模型服务端生成速度提高。
历史采用每页最多 180 个逻辑行的显示窗口；较长行仅为显示折行，不删减源内容。
不再丢弃第 80 条以前的消息或单条 20,000 字符以后的内容，所有内容均可分页查看，导出仍是原始完整内容。
手动浏览历史时，新回复不会抢回滚动位置；Ctrl+End 恢复跟随。

模型上下文与显示分页独立：持久化历史保持完整；发送给模型的工作副本会省略较早的长文件写入参数和长命令脚本，保留明确占位说明。
长会话仍可能增加模型服务端耗时，状态栏显示当前阶段、本轮时间和上下文条数。
磁盘导出和记忆操作也在工作线程执行；状态目录可放在 Linux 文件系统，以避免 WSL 跨文件系统 I/O 的额外成本。

本次复验：114 项通过、2 项阈值相关测试未执行。包含真实 RAG、慢磁盘模拟、1,000 次流式通知合并、
长消息完整性、历史浏览不跳页、后台会话刷新隔离与工作区迁移/冲突保护。
合成压力样本约 120 万字符，旧版处理 1,000 次通知耗时约 1.03 秒；新版合并后的通知及一次页面更新约 0.7 毫秒。
此数值衡量本地通知处理，不是模型接口延迟或真实终端帧率。


## 模型延迟与诊断

示例配置针对当前 DeepSeek 网关的文件/网页任务使用 `THINKING_MODE=disabled`。
需要深度推理时可在 `.env` 设置 `THINKING_MODE=enabled`、`REASONING_EFFORT=low` 或 `high`，重启后生效；这会增加等待时间。
其他服务商不一定支持 `thinking` 扩展，使用 `THINKING_MODE=auto` 可不发送该字段。

`SESSION_TIMEOUT=60` 是网络空闲时限，允许服务商生成较大的工具参数；`MODEL_CALL_TIMEOUT=90` 是一次模型请求从重试到流结束的总时限。
两者不等同于整轮任务时限，一轮任务可能包含多次模型/工具调用及用户确认。
不完整工具流、被截断的输出和无效 JSON 不会进入工具执行。已经执行的工具不会因后续模型超时而自动重放。

状态栏显示当前阶段、本轮耗时和无新数据等待时间；执行详情默认隐藏，可用 `/details on` 临时显示。
`.agent/cli.log` 中的 `model_metrics`、`tool_metrics` 及各会话 `session.json` 的 `last_run` 记录首包、首个可见输出、最大帧间隔、总时长和工具用时。
工具用时包含用户确认等待；字符数不是 token 数。诊断日志不记录思考正文；思考详情保存在私有会话历史中。

可运行 `uv run python -m scripts.diagnose --live --runs 3 --thinking disabled --read-timeout 60` 做真实模型工程探测。
它会使用当前 API 配额，在临时隔离工作区自动确认测试操作；仅保存测试输出至 `.cache/reports/system-diagnostics/`，不修改真实会话。


## 长文件任务与上下文窗口

旧会话恢复后也会收到当前文件工具协议，无需删除历史或手动重建提示词。
`FILE_READ_CHARS` 默认 6000；`MODEL_INPUT_CHARS` 默认 64000；`MODEL_OUTPUT_CHARS` 默认 48000。这些都是字符数量限制，不是模型 token 窗口。
发送前仅处理工作副本，最近两组工具操作保留；更早的大写入参数与脚本会省略，容量紧张时可省略旧读取页，再淘汰完整旧轮次，保持工具调用与结果配对。
省略数量在系统上下文中明确标注，`last_run.context` 记录原始/发送字符数。当前用户请求不会被悄悄截断；仍超限时保存 checkpoint，提示具体预算项与恢复建议。
原始消息、完整工具参数、会话导出和界面历史保持完整。需要文件当前内容时重新分页读取。

流超时、截断或无效调用最多触发一次“小步骤恢复”，不会重放已经执行的工具；第二次失败即停止并保留已完成工作。
每轮默认最多 24 次模型交互，模型每次会看到剩余次数并预留收尾。用尽后显示“阶段保存·可继续”，持久保存为 checkpoint，重启后也可继续；不会宣称任务已经全部完成。

使用 `scripts.diagnose` 的 `--replay-session .agent/<ID>/session.json` 可在临时工作区重放该会话最后一条用户要求。
该选项复制历史与工作区，不修改原始记录；每个测试任务最多 240 秒，测试确认自动批准，输出含私有会话副本，应保留在忽略目录。
详细故障与复验见 [长文件修复报告](large-file-recovery.md)。

后台评估使用独立的 ASSESS_CONCURRENCY 池（默认 1），不占前台模型容量。

审计配置：CLI 的审计按会话写入 `AGENT_STATE_DIR/<id>/audit.jsonl`；`AUDIT_LOG` 仅用于无会话 ToolContext 的独立工具调用。默认提示词统一位于 `src/ai_agent_startup/core/prompts.py`。测试路径为 `tests/`。

## 增量持久化（v2）

`session.json` 保存元数据、系统指令与提交字节偏移，完整消息在 `messages.jsonl` 追加后 fsync，再原子提交索引。
恢复忽略未提交尾部；已保存工具结果不重放。消息追加后不可原地修改（系统指令单独快照）。
旧 v1 文件读取兼容，首次保存迁移前保留 `session.v1.bak`，不会批量修改尚未使用的会话。
导出先等待保存队列，再在线程读取完整提交快照；导出的 JSON 仍包含完整 messages。
`SESSION_MAX_MB=64` 是新轮次准入限额，不丢弃在途工具结果；超限后 `/export` 归档并新建会话，系统不自动删除用户数据。

## 工具调度

连续的 read_file/list_files/rag_search 可并发，默认 TOOL_CONCURRENCY=4；遇写操作边界先收齐结果，写操作仍串行。
每轮最多执行 MAX_TOOL_CALLS_PER_ROUND=8 个调用，超额调用逐一返回拒绝结果，保持原顺序和配对。
Tool 元数据可设置 timeout_s/concurrency，默认 TOOL_TIMEOUT=120 秒。只读超时返回后后台线程仍占读槽直到结束；退出等待收尾。
写工具超时先取消确认/发出停止信号并等待旧线程，才允许下一轮，避免迟到写入；Python 线程不能被强制杀死。

## 记忆条目与检索

`/remember` 去重保存条目（ID、时间、来源、标签和正文）；`/memory list` 查看，`/memory rm <id>` 需 /yes 确认删除。
旧纯 Markdown 仍可读，下一次显式编辑时转换为带元数据的条目行；最多 64 条，正文总量受 MEMORY_MAX_CHARS 限制。
按当前用户问题用 BM25 取 MEMORY_TOP_K=4 条，注入最多 MEMORY_INJECT_CHARS=1200 字符，无命中时仅提供有界摘录。
MEMORY_AUTO_EXTRACT 默认关闭；启用时后台提取 JSON 候选保存到 memory-candidates.json，`/memory candidates` 查看，
通过 `/remember 内容` 明确采纳后才注入。失败不影响回答；候选不等于已核实事实。
MEMORY_SHARED 默认关闭；显式开启后使用状态根的 shared-memory.md，旧会话记忆不搬移、不删除，关闭即可回到隔离文件。

## 备用模型与计量

MODEL_FALLBACKS 默认为空 JSON 数组，可按顺序指定 model/base_url/api_key_env；不同服务地址必须显式指定密钥环境变量。
仅流开始前的连接、限流、5xx 或连接阶段总超时，在当前模型重试耗尽后切换；鉴权/参数错误不切换，已开始输出的流不自动重放。
last_run.model_calls 记录实际 model、fallbacks、input_tokens/output_tokens、tokens_source 与 estimated_cost_usd。
MODEL_STREAM_USAGE=True 时请求服务商 usage；否则按文本和 schema 估算。默认单模型行为不改变。
INPUT_COST_PER_MILLION/OUTPUT_COST_PER_MILLION 为美元每百万 token，均 0 表示未知、成本为 null。
备用条目可独立指定 input_cost_per_million/output_cost_per_million，未配置时不套用主模型价格；重试失败的服务商计费无法精确获知。
