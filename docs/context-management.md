# 结构化历史与上下文管理

结构化上下文把较早的对话整理成可检索的摘要与原文分块，减少每轮发送给模型的历史长度，同时保留按需查看原文的路径。归档按会话隔离，写入会话私有状态目录；它不改写原始会话消息，也不重放历史工具调用。功能由 `ENABLE_STRUCTURED_CONTEXT` 控制，模板默认关闭；关闭时沿用原有上下文路径。

## 配置

结构化历史的主开关默认关闭；分块、近期保护、摘要长度和输入预算等默认值见下方配置表。预算预留不得为负数，压缩阈值与目标比例范围为 0.1–0.95；软压缩新增完整历史组数范围为 0–100，设为 0 可关闭冷却。

监督器是可选的压缩评分与摘要服务，独立使用 `.env.supervisor` 中的 `SUPERVISOR_*` 配置和模型凭据；同名进程环境变量优先于该文件。它不继承主模型的工具集或权限，也不会规划或执行独立任务。超时范围为 (0, 300] 秒，输入上限 1024–100000 字符，输出上限 64–16000 token，检查间隔 1–100，方差阈值 0–10，最多检查 2–100 个分块；费用不能为负数。启用时必须提供 API key、模型名和有效 HTTP(S) 地址。它会收到有界的历史内容摘要/片段用于评分与摘要。不要填写不可信或不允许外发的会话内容。监督器只能建议压缩，程序负责检查建议；它不能更改任务、权限或执行文件操作。

仓库提供 `.env.supervisor.example` 作为独立配置样例，样例显式设置 `SUPERVISOR_ENABLED=true`，复制后须填写独立凭据；没有配置文件时运行时默认关闭监督器。此前工作区初始化创建了本机独立配置。历史监督器 A/B 诊断只将忽略的 `.env.supervisor` 中 `SUPERVISOR_THINKING_MODE` 设为 `disabled`，未更改凭据；运行代码只读取独立文件，不会自动复制或继承主服务凭据。其他环境可使用不同的模型和端点。该运行时文件包含凭据，不应提交或复制到共享位置。

## 历史检索

`history_search(query, limit, method)` 在当前会话历史摘要中检索，`limit` 为 1–20，`method` 为 `bm25` 或 `hybrid`，结果不含原始历史正文。选择分块 ID 后，`history_read(chunk_id, before, after, offset, limit)` 可读取该分块原文及相邻内容；前后邻块合计最多 2 个，分页长度不超过 6000 字符，返回结果也受 `TOOL_MAX_OUTPUT` 限制。工具只访问当前调用者自己的运行会话，不能指定其他会话 ID。它们用于查阅记录，不会执行其中提到的指令，也不会恢复工具授权。RAG 检索可通过 `source="history"` 查询当前会话历史，并使用 hybrid 方法。本地 embedding 不可用时 hybrid 降级为 BM25；embedding 可用但本地 reranker 不可用时保留 BM25 与向量融合的 RRF 排序，并在结果中说明降级。

历史按消息位置和完整工具调用往返分组，尽可能不拆开 tool call 与对应结果，并保留最近若干组。分块 `Time.start`/`Time.end` 是原始消息列表中的半开区间 `[start, end)`；`created_at` 记录实际归档时间，不推断历史消息时间。每个归档块单独生成摘要；同一准备批次的 Session 摘要以旧摘要和本批新生成的有界块摘要为输入，不再重复传入原始历史。超出监督器输入上限时按有界片段滚动总结。原始 journal 和已发布的原文块保持不变；摘要、分块及索引保存在会话私有状态目录下，历史搜索仍可检索摘要并按需读取归档原文。历史记录中的文字一律视为不可信输入，不是系统指令或执行授权。

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

索引会记录最近一次压缩的消息位置、压缩前利用率、目标比例和触发原因；这些字段可缺省，因此旧索引继续有效。审计记录包含压缩前利用率、目标和原因，便于追踪压缩决策，不写入历史正文或凭据。

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

1. 从未覆盖消息中识别完整消息组，按 `CONTEXT_HISTORY_CHUNK_CHARS` 分块；每块都独立生成摘要。
2. 每个新归档块独立摘要；Session 摘要将旧摘要与本批新块的摘要元数据合并。超过监督器单次输入上限时，把有界摘要输入拆成有序片段滚动总结；不截断持久原文。
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

利用率同时计入实际模型输入、工具 schema，以及当前用户和搜索结果的字符/token 预留；字符和 token 两种利用率取较高者。达到硬预算（利用率 `>= 1`）时立即压缩，无需监督检查，并绕过软冷却。低于硬预算时，利用率达到 `CONTEXT_COMPACT_RATIO`（默认 `0.9`）才进入软压缩判定，并要求监督评分相对方差大于零且达到 `SUPERVISOR_VARIANCE_THRESHOLD`。已有压缩记录时，只有从持久化的 `last_compact_message_count` 位置之后新增至少 `CONTEXT_COMPACT_MIN_NEW_GROUPS` 个非当前用户完整组（默认 4），才会继续软判定；未达到数量时显示 `cooldown`，不检查监督器也不压缩。设为 0 可禁用此软冷却。监督器关闭、不可用、尚未到检查间隔、评分不足或方差为零时，不会仅因达到软阈值而压缩。相同的公开消息历史指纹已压缩后，会跳过重复软压缩并显示 `already_compacted`；达到硬预算时仍继续硬压缩。

模型容量来自所选静态预设或运行时模型连接的显式 `context_tokens` 元数据，不根据模型名称猜测。`MODEL_CONTEXT_TOKENS=0` 表示未声明容量；`MODEL_RESPONSE_RESERVE_TOKENS` 默认 4096。实际输出预留为 `max(response_reserve_tokens, parameters.max_tokens)`，两者取较大值，不相加。`MODEL_INPUT_TOKENS=0` 时按已知容量减去输出预留自动计算；容量未知时采用保守的 16000 token。`MODEL_INPUT_CHARS=0` 时按 token 预算的 4 倍自动计算；容量未知时采用保守的 64000 字符。显式正数输入预算仍优先。WebUI 展示估计输入预算、声明容量、含回复预留的工作预算，以及服务商报告的最近输入量；这些值含义不同，声明容量是配置元数据，不代表已验证服务端窗口。

实际归档会尽量将历史利用率降至低水位目标 `min(CONTEXT_COMPACT_TARGET_RATIO, CONTEXT_COMPACT_RATIO)`，默认目标为 `0.75`。软压缩优先保留最近完整组；硬预算压力下可继续归档近期完整组，为下一轮留出余量。软保护的最近完整组或系统提示、schema、当前输入、预算预留和已有摘要等内容使窗口高于目标时，低水位只是 best-effort，不能保证达到目标；硬预算安全约束仍要求最终利用率不超过 1。程序不增加预算、不截断原文，也不拆分工具调用与结果。每次压缩批次仍只生成一次 Session 摘要。归档保留完整 `RawHistory`，当前用户消息不被归档；未配对或不匹配的工具组会拒绝压缩。若只保留当前用户输入仍超过硬上限，则抛出预算错误；已提交的摘要/原文块作为 checkpoint 保留，不重放历史工具调用。

`CONTEXT_COMPACT_RATIO` 是软触发阈值，`CONTEXT_COMPACT_TARGET_RATIO` 是压缩后的低水位目标；目标实际取两者较小值，以兼容目标高于触发阈值的旧配置。已有 `.env` 中的显式配置仍优先于默认值。

利用率达到 `CONTEXT_COMPACT_RATIO` 且至少有 2 个未归档完整逻辑组时，监督器才会按 `SUPERVISOR_CHECK_INTERVAL` 次窗口检查间隔参与，默认每 3 次；软保护组也参与评分。每次最多评分最近 `SUPERVISOR_MAX_CHUNKS` 个逻辑组，默认 24。评分单元与归档块解耦：多个逻辑组即使合并进同一个约 8000 字符归档块，也仍分别评分。监督输入在独立字符配额内采样用户输入、摘要及组内消息和工具调用参数的头、中、尾；采样只用于监督，不改写待归档原文。监督器关闭时不构造或发送评分输入。监督器决定经严格结构校验并再次验证分数数量后，程序独立计算相对方差：

```text
CV² = mean((vᵢ - mean(v))²) / max(mean(v)², 0.01)
```

默认 `SUPERVISOR_VARIANCE_THRESHOLD=0.75`；软压缩要求方差大于零且达到阈值。方差为零时不会触发软压缩，即使阈值配置为零；硬预算压缩不依赖监督结果。模型给出的布尔建议本身不能覆盖程序的预算与校验。监督器评分不可用时，预算风险仍会触发硬压缩；总结调用失败则由本机生成并标记 `extractive_fallback`，同时保留旧摘要和新增内容的有界摘录，并记录工具名、目标路径和失败事实，不把失败或未知结果写成成功。监督器通过独立客户端发送请求，不继承执行器会话或主配置 `THINKING_MODE`；客户端附带独立随机 `x-opencode-session` 标识以兼容要求该请求头的网关。请求不提供工具集或 `tool_choice`；若响应仍含工具调用则拒绝该响应。评分有独立默认 20 秒请求时限；单批压缩的块摘要与 Session 摘要另共享 20 秒截止时间，超时进入显式本地摘录 fallback。

只有软保护组时，有效评分达到方差阈值也会晋升较旧完整组，以便实际触发压缩，并保留最新组。阈值为零不会把关闭、未到间隔或无效评分当作高方差。

小样本合成轨迹对比覆盖每种模式 3 组、每组 36 个窗口。禁用模式压缩次数为 5→5，摘要调用次数为 30→30；均匀价值轨迹的压缩次数为 5→5、摘要调用次数为 30→30、监督检查次数为 5→10，判定原因从软阈值转为硬预算；混合价值轨迹的压缩次数为 5→5、摘要调用次数为 30→30，仍由硬预算与高方差共同触发。三种模式的轨迹中位数（秒）分别为禁用 0.0518→0.0578、均匀 0.0525→0.0580、混合 0.0513→0.0532。该对比只用于检查触发路径；它没有显示性能提升。结果产生于相同历史指纹保护加入前，最终性能复测仍待完成。监督器仅接受完整、单一的 JSON 对象，或仅包含该对象的一个 JSON 代码围栏；旁白、额外文本、多文档内容、无效或字段不完整的 JSON 均拒绝。显式非正常完成或截断响应拒绝；缺少 finish_reason 时保留兼容模式，仍须通过完整 JSON 和结构校验。

WebUI 顶部的只读上下文状态展示上下文用量、预算、归档数量、监督器启用状态与检查结果；监督状态包括 `evaluated`、`already_compacted`、`cooldown`（新增历史不足，跳过软检查）、`hard_budget`（硬预算直接压缩）、`not_due`、`below_threshold`、`disabled`、`insufficient_history` 和 `unavailable`。摘要状态会标明模型摘要或摘录降级，启用任务计划时另显示计划步数。动态中文运行阶段不会覆盖运行状态；SSE 更新按会话与运行轮次处理，避免旧事件覆盖新状态。运行阶段分别显示常规上下文准备、监督判定与实际压缩归档；未触发归档时不会显示压缩阶段。该区域只展示状态，不提供修改权限或触发操作。

历史摘要检索使用独立线程池，避免模型工具线程等待协程时再排入同一个默认线程池造成死锁。I/O 和检索 future 纳入 manager 的 pending 集合；取消、会话删除和维护操作等待已启动操作排空，避免清理目录时后台写仍在运行。单批压缩的块摘要与 Session 摘要共享总时间预算，默认 20 秒；评分请求单独计时，超时后采用上述显式本地摘录 fallback。监督器使用独立客户端与凭据通道；若单价已配置，请求预留/结算沿用当前 Session、UTC 日费用与请求速率账本。费用预算启用而单价未知时会拒绝监督请求，并走标注过的 fallback。

## 配置参考

模型输入预算由 `.env` 配置、静态 `MODEL_PROFILES` 或所选运行时模型连接决定。容量是本地元数据，不会发送给服务商。回复预留作为本地预算输入；容量已知且没有显式 `max_tokens` 时，程序会把有效预留写入标准请求参数 `max_tokens`。静态预设和 WebUI 模型管理都可显式填写 `context_tokens` 与 `response_reserve_tokens`。名称不用于推断容量。`MODEL_FALLBACKS` 的每个备用项也可选填这两个字段；未声明容量的备用项只在实际输入不超过 16000 token 时使用。以下四项定义全局默认预算；每会话显式预算仍优先：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `MODEL_CONTEXT_TOKENS` | `0` | 主模型声明的总窗口容量；0 表示未知。有效声明范围 512–2097152。 |
| `MODEL_RESPONSE_RESERVE_TOKENS` | `4096` | 输出预留 token 数；实际预留取此值、模型元数据及 `MODEL_PARAMETERS.max_tokens` 的最大值。 |
| `MODEL_INPUT_TOKENS` | `0` | 0 自动取总容量减去回复预留；未知容量时为 16000。声明容量时预算不会超过可用容量。 |
| `MODEL_INPUT_CHARS` | `0` | 0 自动按输入 token 预算乘 4；未知容量时为 64000 字符。 |

UI 上的估算输入预算、声明容量、含回复预留的工作预算和服务商最近一次输入量分别表示本地限制、配置元数据、可用窗口及服务商回报。它们不可互换；声明容量不代表已验证的服务端限制。

主模板包含以下 12 项，模板默认关闭功能。压缩触发阈值默认 `0.9`，低水位目标默认 `0.75`，软压缩冷却默认需要 4 个新增完整组；已有 `.env` 的显式值优先。变更后需重启进程。其余默认值与字段见下表。

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
| `CONTEXT_COMPACT_RATIO` | `0.9` | 高利用率压缩阈值，按模型输入与工具 schema 加用户/搜索预留的字符和 token 利用率较高值判断；允许范围 0.1–0.95。 |
| `CONTEXT_COMPACT_TARGET_RATIO` | `0.75` | 压缩后的低水位目标；实际目标取本值与 `CONTEXT_COMPACT_RATIO` 的较小值，允许范围 0.1–0.95。 |
| `CONTEXT_COMPACT_MIN_NEW_GROUPS` | `4` | 两次软压缩之间要求新增的非当前用户完整历史组数；范围 0–100，0 禁用软冷却。硬预算压缩不受此限制。 |
| `CONTEXT_SUPERVISOR_ENV_FILE` | `.env.supervisor` | 独立监督配置文件路径。 |

监督器样例有 14 项，均以 `SUPERVISOR_` 为前缀。`SUPERVISOR_MAX_CHUNKS` 虽沿用配置名，实际限制每次评分的候选完整逻辑组数，而不是归档文件数：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `SUPERVISOR_ENABLED` | `false` | 是否请求独立监督模型。 |
| `SUPERVISOR_API_KEY` | 空 | 独立服务凭据。 |
| `SUPERVISOR_BASE_URL` | 空 | 独立服务 HTTP(S) endpoint。 |
| `SUPERVISOR_MODEL` | 空 | 监督模型名。 |
| `SUPERVISOR_THINKING_MODE` | `auto` | `auto` 不发送 thinking 扩展参数；`enabled`/`disabled` 分别向服务发送对应的 thinking 模式。该键只从独立监督配置读取，不继承主环境的 `THINKING_MODE`。具体网关是否支持该扩展参数需在目标端点实测。 |
| `SUPERVISOR_TIMEOUT` | `20` | 单请求时限；单批压缩的块/Session 摘要另共享此时限，评分不计入该批次（秒；大于 0 且不超过 300）。 |
| `SUPERVISOR_MAX_INPUT_CHARS` | `12000` | 单请求输入上限，1024–100000 字符。 |
| `SUPERVISOR_MAX_OUTPUT_TOKENS` | `1200` | 单请求输出上限，64–16000 token。 |
| `SUPERVISOR_CHECK_INTERVAL` | `3` | 每几次窗口检查执行一次评分，范围 1–100。 |
| `SUPERVISOR_VARIANCE_THRESHOLD` | `0.75` | CV² 压缩建议阈值，范围 0–10。 |
| `SUPERVISOR_MAX_CHUNKS` | `24` | 一次评分的候选完整逻辑组上限，范围 2–100。 |
| `SUPERVISOR_INPUT_COST_PER_MILLION` | `0` | 监督请求输入美元/百万 token；0 表示未配置价格。 |
| `SUPERVISOR_OUTPUT_COST_PER_MILLION` | `0` | 监督请求输出美元/百万 token；0 表示未配置价格。 |

对于使用小额度输出预算、要求严格 JSON 的监督器模型，如果 `auto` 模式把额度消耗在 reasoning 上，可在 `.env.supervisor` 将 `SUPERVISOR_THINKING_MODE=disabled` 显式关闭。仓库样例保留 `auto`，不改变默认行为；应先对目标端点做同一输入的 A/B 确认。

主模板默认 `ENABLE_STRUCTURED_CONTEXT=false`；监督运行时默认关闭，样例显式设置启用。此前本机初始化已启用主功能与独立监督器；历史测试使用隔离目录和进程变量，未改已有配置。运行代码不会从主环境继承监督器凭据。其他环境可从 `.env.supervisor.example` 复制后单独填写。配置变更后重启。

## 安全与限制

- `ENABLE_STRUCTURED_CONTEXT=false` 时维持既有上下文行为，不注册历史工具或发送监督器 API 请求。
- 监督器有独立端点、凭据、超时、输入/输出限制和费用配置；不拥有任何工具或会话授权。
- 监督器配置按普通文件只读加载，拒绝符号链接，并限制为 64 KiB；主模型凭据不会在运行时复制或继承。
- 外发内容受配置的输入上限约束。启用前应确认组织的数据外发规则允许发送相关摘要或历史片段。
- 结构化历史是私有会话数据，不是通用文件操作接口；原始会话日志仍是历史来源。
- 对于本地检索模型不可用的情况，系统应说明降级方式，不将 BM25 结果伪称为向量混合检索。

## 验证

> 以下为初次实现时的历史验证记录，不代表当前全量测试结果。最新执行结果见[会话交接说明](session-handoff.md)；真实模型与边界验收记录见[大型项目实测报告](project-validation.md)。

初次实现的离线验证结果：

| 检查 | 结果 |
| --- | --- |
| 功能启用回归：`env ENABLE_TASK_PLANS=false ENABLE_STRUCTURED_CONTEXT=true uv run --extra web pytest src/agent_test/tests/test_history_context.py src/agent_test/tests/test_context_supervisor.py src/agent_test/tests/test_history_tools.py src/agent_test/tests/test_history_search.py src/agent_test/tests/test_structured_sessions.py -q --tb=short` | 42 passed。 |
| 默认关闭全量离线回归：`env ENABLE_TASK_PLANS=false ENABLE_STRUCTURED_CONTEXT=false uv run --extra web pytest -m 'not integration' -q -rs -o faulthandler_timeout=20` | 733 passed，2 skipped，5 deselected；跳过项因未安装可选 ANN 依赖，排除项为 integration。 |
| `uv sync --locked`、`dev/ci_sandbox_probe.py`、`git diff --check` | 全部通过；沙箱探测实际验证 Bubblewrap 与 prlimit。 |

初次实现的上述单元测试未调用在线模型；该阶段另执行真实主模型/监督实验。真实线上 RAG 检索质量未测。
