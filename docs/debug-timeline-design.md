# DEBUG 因果时间线

> 状态：本文记录新版总览设计与当前数据边界。此前截图针对旧版全通信/焦点筛选布局，已被整体行为、事件与异常总览替换；真实模型长任务的因果记录见[博客任务报告](blog-debug-real-model-report.md)。

## 目标与呈现

WebUI 调试页默认呈现根 Agent 的整体行为、事件和异常总览。根 Agent 行高 224 px。每个父 Agent 共享一组左右分流/汇流母线，覆盖其后代的工作范围，避免旧嵌套 trace 的连接线相交；母线表达父子调用结构，不表达耗时，也不改变当前递归代理执行限制。子 Agent 默认行高 72 px，仅显示其真实存续区间；点击标签展开为 224 px 的完整行为时间线，再次点击收起。活跃子 Agent 不显示 join。每个子 Agent 只在自身行内、真实起止点之间显示水平分流／汇流连接段，不再绘制单独的跨行长曲线。行为标记使用避开母线通道的短竖箭头；Plan 和具体工具优先显示，Think / Loop 留在详情中。模块 span 显式展开后显示在独立细节区，可增加轨道高度。上下文与 token 曲线保留。

默认视图不绘制任何 message-edge。工具栏“查看通信详情”打开独立路由详情区，由 `#communication-routes` 按消息显示 sender→recipient、发送时间、读取时间和 pending 状态。回放中局部通信共 6 条，其中 2 条在收件 Agent 结束后发送；详情如实显示为未读 pending。页面只据已有发送/读取事件陈述状态，不推断消息已送达或已读取。保留 sent/read 事件分页与显式读取正文入口。行为异常在精确时间位置显示红/橙色点，并可从异常入口定位；摘要分别计工具错误、拒绝和其它异常。示例 trace 摘要为 5 个工具错误、1 个拒绝、Compact 0；原始异常记录共 18 条，是独立于去重摘要的另一个计数口径。tool 与 span 指向同一事故时，按含 session、turn、tool_call_id 的事故 ID 去重；跨轮复用的 tool_call_id 不会合并，原始事件仍可打开。

活动 Agent 仍以细线上的低调流动标记提示状态。父子调用各自显示真实起止时间，耗时可能互相包含，不可相加。既有通信只允许直接父子 Agent，时间线不扩展 sibling 通信权限。

默认开启的空闲压缩只折叠由轮次/Agent 停止与后续开始事件夹定的全局空闲段，并排除有活动区间跨越的间隔。等待模型或工具的耗时保留在时间轴上。折叠段标注真实时长，横轴不再等距；关闭“压缩空闲”可查看等距时间轴。暂停页面刷新及系统减少动态效果设置会停止动画。

```text
时间 →   根 Agent（224 px）：行为 / 事件 / 异常
         ├── 共享分流母线 ──┬── 共享汇流母线
         子 Agent A（72 px）：真实 workline ── 点击展开行为（224 px）
         子 Agent B（72 px）：真实 workline ── 活跃时保持开放
         行内水平分流／汇流连接段；母线表示结构，不表示耗时
         通信计数 → “查看通信详情” → #communication-routes
```

并发 span 各自显示真实起止时间，父子调用耗时可能重叠，不可相加。通信线表示发送与接收关系，不代表接收方从该时刻才开始执行。现有通信只允许直接父子 Agent，时间线不扩展 sibling 通信权限。

## 数据与接口

- `DEBUG=true` 启用采集；关闭时不分配时间线事件，也不为图表额外调用 LLM。
- `DEBUG_TRACE_MAX_EVENTS` 同时限制时间线事件、legacy span 窗口和活动 span，默认 512，范围 1..10000。容量淘汰计数可见；Agent 显示数量也受该容量限制。
- 每次服务进程启动生成新的 `generation`；事件按发布顺序分配单调递增 `seq`。`/api/debug` 支持 `(generation, cursor)` 增量读取。游标落后于保留窗口会标记 gap；generation 不同或游标无效会标记 reset。客户端遇 gap/reset/重启后重取完整当前窗口并清理旧选择。服务重启后旧窗口无法恢复。
- 采集固定事件类型、状态、时间、因果 ID 和白名单指标；不把 prompt、reasoning、完整命令或工具参数写入普通事件。reasoning 流只更新活动标记，不逐 token 产生日志事件。
- 事件详情仅在用户选择后按需从当前会话读取。API 每页最多 6000 字符，详情总量上限 1 MiB；返回前对已知密钥和凭据脱敏。详情只在事件仍处于当前保留窗口、generation 匹配时可用；重启或淘汰后返回 410。详情不提供 reasoning。
- 导出在浏览器本地生成当前快照，只含元数据，不含按需加载的正文。

## 指标解释

上下文利用率按历史上下文与预留空间计算，包含当前用户输入及搜索的字符/token 预留，并按字符预算和 token 预算中的较高占用显示。因此它表示估算的上下文预算占用，不是单纯历史文本比例。Compact 前后采样分别作为事件呈现。

token 指标按请求或累计显示 input/output，并标注来源：`provider` 是服务商报告值，`estimated` 是本地估算，`unknown` 缺少可靠数据时留空且断线，不绘为 0。token 事件只从 DEBUG 开始采集，不代表账单或启用前的完整历史。服务端不为调试额外请求模型。

## 界面操作

调试入口为 `/debug`，需要设置 `DEBUG=true` 并重启 WebUI。页面支持时间线缩放、跟随最新/回到最新、子 Agent 展开、工具批次展开、选择事件后加载详情和分页、打开通信详情，以及导出元数据快照。默认总览不绘制全局通信线，通信计数入口打开后按条查看关系；旧版全通信与焦点筛选说明及截图验收已由当前设计替换。暂停会停止页面刷新与请求，但 Agent 和服务端采集继续运行；恢复时依 generation/cursor 状态增量追赶，若发生 gap 则重建当前窗口。

## 实施文件

| 文件 | 已实现内容 |
| --- | --- |
| `src/ai_agent_startup/core/debug_runtime.py` | 有界事件与 span 缓存、generation/seq、增量窗口/gap/reset、白名单元数据。 |
| `src/ai_agent_startup/core/sessions.py`、`src/ai_agent_startup/core/agent_tasks.py`、`src/ai_agent_startup/core/communication.py` | 轮次、工具、任务、Agent、消息和阶段边界事件。 |
| `src/ai_agent_startup/core/llm.py`、`src/ai_agent_startup/core/history_context.py` | token usage 来源和上下文/Compact 指标事件。 |
| `src/ai_agent_startup/web/app.py`、`src/ai_agent_startup/web/debug_details.py` | 只读时间线快照、增量游标和按需脱敏详情 API。 |
| `src/ai_agent_startup/web/static/debug-view.js` | Agent lane、工具批次、通信、并发区间和曲线视图数据。 |
| `src/ai_agent_startup/web/static/debug.js`、`src/ai_agent_startup/web/static/debug.html`、`src/ai_agent_startup/web/static/debug.css` | 时间线交互、页面刷新、详情分页、筛选缩放及响应式界面。 |

## 验收边界

既有截图和布局测量只验证了旧版并列 Agent 轨道及默认通信箭头，已被新版总览设计替换，不作为当前布局验收。当前布局依据既有 4460 条事件、3 个 Agent 的真实记录回放；这不是本轮新模型调用。边界包括：真实 workline 起止与活跃状态、分流/汇流结构不暗示耗时、通信只反映实际发送/读取事件、工具错误/拒绝/其它异常分开计数且关联重复事故去重、原始事件可访问、gap/reset 重建一致窗口、详情在淘汰/重启后失效、未知 token 留空。暂停只冻结视图，不暂停 Agent。常规 trace 与导出只含元数据；显式选择详情才读取会话消息，且经过脱敏与分页。旧窗口不跨进程重启保留，token 曲线不补齐 DEBUG 启用前的历史。
