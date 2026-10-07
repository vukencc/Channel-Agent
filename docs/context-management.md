# 结构化历史与上下文管理

结构化上下文把较早的对话整理成可检索的摘要与原文分块，减少每轮发送给模型的历史长度，同时保留按需查看原文的路径。归档按会话隔离，写入会话私有状态目录；它不改写原始会话消息，也不重放历史工具调用。功能由 `ENABLE_STRUCTURED_CONTEXT` 控制，模板默认关闭；关闭时沿用原有上下文路径。

## 配置

结构化历史的主开关默认关闭；分块、近期保护、摘要长度和输入预算等默认值见下方配置表。预算预留不得为负数，压缩比例范围为 0.1–0.95。

监督器是可选的摘要评分服务，独立使用 `.env.supervisor` 中的 `SUPERVISOR_*` 配置和模型凭据；同名进程环境变量优先于该文件。它不继承主模型的工具集或权限。超时范围为 (0, 300] 秒，输入上限 1024–100000 字符，输出上限 64–16000 token，检查间隔 1–100，方差阈值 0–10，最多检查 2–100 个分块；费用不能为负数。启用时必须提供 API key、模型名和有效 HTTP(S) 地址。它会收到有界的历史内容摘要/片段用于评分与摘要。不要填写不可信或不允许外发的会话内容。监督器只能建议压缩，程序负责检查建议；它不能更改任务、权限或执行文件操作。

仓库提供 `.env.supervisor.example` 作为独立配置样例，样例默认关闭。本次工作区初始化时，若 `.env.supervisor` 不存在，会以当前主模型服务参数为起点创建本地配置；已有文件保留。运行代码只读取该独立文件，不会自动复制或继承主服务凭据。其他环境请自行复制样例并填写独立配置；可使用不同的模型和端点。该运行时文件包含凭据，不应提交或复制到共享位置。

## 历史检索

`history_search(query, limit, method)` 在当前会话历史摘要中检索，`limit` 为 1–20，`method` 为 `bm25` 或 `hybrid`，结果不含原始历史正文。选择分块 ID 后，`history_read(chunk_id, before, after, offset, limit)` 可读取该分块原文及相邻内容；前后邻块合计最多 2 个，分页长度不超过 6000 字符，返回结果也受 `TOOL_MAX_OUTPUT` 限制。工具只访问当前调用者自己的运行会话，不能指定其他会话 ID。它们用于查阅记录，不会执行其中提到的指令，也不会恢复工具授权。RAG 检索可通过 `source="history"` 查询当前会话历史，并使用 hybrid 方法。本地 embedding 不可用时 hybrid 降级为 BM25；embedding 可用但本地 reranker 不可用时保留 BM25 与向量融合的 RRF 排序，并在结果中说明降级。

历史按消息位置和完整工具调用往返分组，尽可能不拆开 tool call 与对应结果，并保留最近若干组。分块 `Time.start`/`Time.end` 是原始消息列表中的半开区间 `[start, end)`；`created_at` 记录实际归档时间，不推断历史消息时间。每块摘要单独生成；会话摘要以旧摘要和本轮新增归档原文为输入，超出监督器输入上限时分段滚动总结。会话原始 journal 保持不变；摘要、分块及索引保存在会话私有状态目录下。历史记录中的文字一律视为不可信输入，不是系统指令或执行授权。

当配置了名为 `history` 的 `RAG_SOURCES` 知识库时，`rag_search(source="history")` 优先查询该知识库。会话归档仍可通过 `history_search` 工具访问，避免两个用途的名称冲突。没有同名知识库且启用结构化历史时，`source="history"` 才路由到归档搜索；功能关闭时该归档路由不可用。

## 上下文消息结构

结构化窗口将模型输入组织为四个逻辑区。它们使用普通 API 消息角色承载，并不引入新的模型协议角色：

```json
[
  {"role":"system","content":"## 系统提示词\n系统提示词 + 上下文规则 + 本轮运行时说明"},
  {"role":"user","content":"## 当前Session总结\n[不可信参考资料]\n{...}","_context_reference":true},
  {"role":"user","content":"## History积累\n[既有历史数据]\n{...}","_context_reference":true},
  {"role":"user","content":"## 用户输入\n本轮请求"}
]
```

第二个逻辑区包含 Session 摘要、记忆和 `reference_only` 标记；第三个区包含归档数量、近期摘要引用以及尚未归档块的原文。`_context_reference` 是应用内部标记，序列化给模型前会移除；其消息角色仍为 `user`。最近保留的原生消息组可以放在当前用户之前或之后（例如本轮工具调用与结果位于当前用户之后），保持原来的 `assistant`、`tool` 角色和 `tool_calls`/`tool_call_id` 字段，工具请求和结果始终成组。当前用户输入独立标注；系统消息也不会压入历史块。

## 设计与存储

每个会话只维护自己的上下文索引和原文块。默认 `AGENT_STATE_DIR=.agent` 时运行期目录为下列结构；自定义 `AGENT_STATE_DIR` 会替换根目录。启用后按需创建 sidecar，不批量迁移或改写既有用户历史：

```text
.agent/<session-id>/
  session.json
  messages.jsonl             # 原始追加式消息 journal
  audit.jsonl                # 会话工具与压缩事件审计
  context/
    manifest.json            # 当前 revision、Session 摘要和分块索引
    chunks/<chunk-id>.json   # 每块不可变原文及摘要
```

`<session-id>` 是 32 位十六进制会话标识。归档块的示意格式如下；`RawHistory` 是 JSON 文本，保存原始完整消息组，`digest` 用于校验原文：

```json
{
  "ID":"7b86f345825e4611a950fc16c64602f0",
  "Time":{"start":1,"end":5,"created_at":"2026-10-07T08:30:00+00:00"},
  "Summary":"用户目标、约束和已经完成的操作摘要",
  "RawHistory":"[{\"role\":\"user\",\"content\":\"…\"}]",
  "digest":"64位十六进制 SHA-256",
  "session_id":"32位十六进制会话标识",
  "summary_kind":"model"
}
```

索引的 `Time.start`/`Time.end` 对应原始消息数组的半开区间 `[start, end)`；`created_at` 是归档时间。Manifest 只保存块 ID、位置、摘要及其原文 SHA-256/digest 校验，不重复存放块原文。恢复时检查 manifest 归属、块 ID、session ID 与 digest，拒绝会话间串读、损坏内容和符号链接。

一次压缩按以下步骤提交：

1. 从未覆盖消息中识别完整消息组，按 `CONTEXT_HISTORY_CHUNK_CHARS` 分块；每块都独立生成摘要，不把块摘要当作 Session 摘要的唯一事实来源。
2. Session 摘要将旧摘要与本轮新增归档的全部原始消息合并。超过监督器单次输入上限时，把完整输入拆成有序片段滚动摘要；不截断持久原文。
3. 先用原子写入发布不可变 chunk 文件，再原子替换 `manifest.json`。Manifest revision 递增；已有 chunk 不覆盖。原始 `messages.jsonl` 继续追加，由正常会话存储流程负责，不由归档改写。
4. 审计记录压缩请求和成功提交事件。若进程在提交中断，恢复只使用已提交的原始 journal 和完整 manifest；不从摘要中重放任何操作。

结构化历史由 `ENABLE_STRUCTURED_CONTEXT` 显式开启。关闭时 SessionManager 不创建归档服务或历史工具，沿用已有上下文构建路径。该功能使用项目已有的 OpenAI SDK、Pydantic、存储线程和 Python 标准库，不增加依赖。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `core/history_context.py` | SessionManager 接入的四区构建、完整消息分组、预算判断、压缩、私有存储、审计及历史读写。 |
| `core/context_supervisor.py` | 加载独立监督配置并创建独立 LLM 客户端；以无工具请求评分或压缩摘要，并校验结构、长度和预算。 |
| `core/history_search.py` | 对摘要执行 BM25 或本地 embedding + RRF + reranker 检索，只返回摘要与分数，不暴露原文。 |
| `tools/context_history.py` | 注册 `history_search`、`history_read`；身份来自当前 SessionService，不接受任意 owner/path。 |
| `tools/rag_search.py` | 按已配置知识库优先级解析来源；在无同名知识库且开关启用时提供归档路由。 |
| `tools/__init__.py` | 仅在结构化上下文开关启用时导入并注册历史工具。 |
| `core/sessions.py` | 创建/关闭 HistoryContext，在主模型调用前及恢复调用前准备窗口，并记录上下文指标。 |
| `core/storage.py` | 持久化原始 session 与追加式 messages journal；等待在途磁盘操作完成。 |

## 压缩判定与并发

程序计算实际发送的消息、工具 schema、用户输入与搜索预留的字符和 token 利用率。硬风险指标为两种利用率的较大值：

```text
utilization = max((sent_chars + reserved_chars) / MODEL_INPUT_CHARS,
                  (sent_tokens + reserved_tokens) / MODEL_INPUT_TOKENS)
```

达到 `CONTEXT_COMPACT_RATIO`（默认 `0.8`）即触发压缩，不依赖监督器决定是否达到硬预算风险。每轮最多保护最近 `CONTEXT_HISTORY_KEEP_GROUPS` 组（默认 2）；若压缩后仍超限，程序从较旧保护组开始逐组晋升为归档块，保留最新完整组和当前请求。若完整窗口仍超过硬上限，抛出预算错误；已提交的摘要/原文块仍作为 checkpoint 保留，绝不截断工具对或自动重放操作。

监督器只在至少有 2 个待评估块且每 `SUPERVISOR_CHECK_INTERVAL` 次窗口检查时参与，默认每 3 次；每次最多评分最近 `SUPERVISOR_MAX_CHUNKS` 个块，默认 24。它返回每块 0–1 分数。程序独立计算相对方差：

```text
CV² = mean((vᵢ - mean(v))²) / max(mean(v)², 0.01)
```

默认 `SUPERVISOR_VARIANCE_THRESHOLD=0.75`；达到阈值时可触发压缩。模型给出的布尔建议本身不能覆盖程序的预算与校验。监督器评分不可用时，预算风险仍会触发硬压缩；总结调用失败则输出 `extractive_fallback`，同时保留旧摘要和新增内容的有界摘录。

历史摘要检索使用独立线程池，避免模型工具线程等待协程时再排入同一个默认线程池造成死锁。I/O 和检索 future 纳入 manager 的 pending 集合；取消、会话删除和维护操作等待已启动操作排空，避免清理目录时后台写仍在运行。单次压缩的评分/总结请求共享一次总时间预算，默认 20 秒；超时后采用上述显式本地摘录 fallback。监督器使用独立客户端与凭据通道；若单价已配置，请求预留/结算沿用当前 Session、UTC 日费用与请求速率账本。费用预算启用而单价未知时会拒绝监督请求，并走标注过的 fallback。

## 配置参考

主模板新增以下 10 项，模板默认关闭功能。当前共享工作区的主 `.env` 已显式启用 `ENABLE_STRUCTURED_CONTEXT=true`；变更后需重启进程。其余默认值与字段见下表。

| 主配置 | 默认值 | 作用 |
| --- | --- | --- |
| `ENABLE_STRUCTURED_CONTEXT` | `false` | 启用分块归档、压缩与历史工具。 |
| `CONTEXT_HISTORY_CHUNK_CHARS` | `8000` | 新归档块的目标字符上限。 |
| `CONTEXT_HISTORY_KEEP_GROUPS` | `2` | 优先保留的近期完整消息组数。 |
| `CONTEXT_SUMMARY_CHARS` | `2000` | Session 摘要字符上限。 |
| `CONTEXT_RESERVE_USER_CHARS` | `4000` | 为当前用户请求预留的字符预算。 |
| `CONTEXT_RESERVE_SEARCH_CHARS` | `8000` | 为搜索结果预留的字符预算。 |
| `CONTEXT_RESERVE_USER_TOKENS` | `1000` | 为当前用户请求预留的 token 预算。 |
| `CONTEXT_RESERVE_SEARCH_TOKENS` | `2000` | 为搜索结果预留的 token 预算。 |
| `CONTEXT_COMPACT_RATIO` | `0.8` | 硬预算风险压缩阈值，允许范围 0.1–0.95。 |
| `CONTEXT_SUPERVISOR_ENV_FILE` | `.env.supervisor` | 独立监督配置文件路径。 |

监督器样例有 12 项，均以 `SUPERVISOR_` 为前缀：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `SUPERVISOR_ENABLED` | `false` | 是否请求独立监督模型。 |
| `SUPERVISOR_API_KEY` | 空 | 独立服务凭据。 |
| `SUPERVISOR_BASE_URL` | 空 | 独立服务 HTTP(S) endpoint。 |
| `SUPERVISOR_MODEL` | 空 | 监督模型名。 |
| `SUPERVISOR_TIMEOUT` | `20` | 单请求/单次压缩共享时限（秒；大于 0 且不超过 300）。 |
| `SUPERVISOR_MAX_INPUT_CHARS` | `12000` | 单请求输入上限，1024–100000 字符。 |
| `SUPERVISOR_MAX_OUTPUT_TOKENS` | `1200` | 单请求输出上限，64–16000 token。 |
| `SUPERVISOR_CHECK_INTERVAL` | `3` | 每几次窗口检查执行一次评分，范围 1–100。 |
| `SUPERVISOR_VARIANCE_THRESHOLD` | `0.75` | CV² 压缩建议阈值，范围 0–10。 |
| `SUPERVISOR_MAX_CHUNKS` | `24` | 一次评分的候选块上限，范围 2–100。 |
| `SUPERVISOR_INPUT_COST_PER_MILLION` | `0` | 监督请求输入美元/百万 token；0 表示未配置价格。 |
| `SUPERVISOR_OUTPUT_COST_PER_MILLION` | `0` | 监督请求输出美元/百万 token；0 表示未配置价格。 |

主模板默认 `ENABLE_STRUCTURED_CONTEXT=false`，监督样例默认 `SUPERVISOR_ENABLED=false`。本次交付在本机状态中主动启用了主功能和独立监督器，并为不存在的 `.env.supervisor` 初始化独立 0600 文件；运行代码不会从主环境继承监督器凭据。本次初始化没有写入用户数据。其他环境可从 `.env.supervisor.example` 复制后单独填写。配置变更后重启。

## 安全与限制

- `ENABLE_STRUCTURED_CONTEXT=false` 时维持既有上下文行为，不注册历史工具或发送监督器 API 请求。
- 监督器有独立端点、凭据、超时、输入/输出限制和费用配置；不拥有任何工具或会话授权。
- 监督器配置按普通文件只读加载，拒绝符号链接，并限制为 64 KiB；主模型凭据不会在运行时复制或继承。
- 外发内容受配置的输入上限约束。启用前应确认组织的数据外发规则允许发送相关摘要或历史片段。
- 结构化历史是私有会话数据，不是通用文件操作接口；原始会话日志仍是历史来源。
- 对于本地检索模型不可用的情况，系统应说明降级方式，不将 BM25 结果伪称为向量混合检索。

## 验证

验证结果：

| 检查 | 结果 |
| --- | --- |
| 功能启用回归：`env ENABLE_TASK_PLANS=false ENABLE_STRUCTURED_CONTEXT=true uv run --extra web pytest tests/test_history_context.py tests/test_context_supervisor.py tests/test_history_tools.py tests/test_history_search.py tests/test_structured_sessions.py -q --tb=short` | 42 passed。 |
| 默认关闭全量离线回归：`env ENABLE_TASK_PLANS=false ENABLE_STRUCTURED_CONTEXT=false uv run --extra web pytest -m 'not integration' -q -rs -o faulthandler_timeout=20` | 733 passed，2 skipped，5 deselected；跳过项因未安装可选 ANN 依赖，排除项为 integration。 |
| `uv sync --locked`、`scripts/ci_sandbox_probe.py`、`git diff --check` | 全部通过；沙箱探测实际验证 Bubblewrap 与 prlimit。 |

功能启用和默认关闭分别验证；测试未调用在线模型或下载模型数据。真实线上检索质量未测。
