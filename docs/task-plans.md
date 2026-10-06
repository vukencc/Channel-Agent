# 任务计划使用说明

任务计划让 Agent 维护一份可检查的工作进度图。它与程序维护的运行状态分开：计划记录目标、步骤、依赖和证据；它不会自动调度步骤、启动模型、授予权限或代替现有子 Agent/后台命令队列。结构化自动验收、多计划切换和递归委派尚未实现。

## 启用与边界

在 `.env` 中设置：

```dotenv
ENABLE_TASK_PLANS=true
TASK_PLAN_MAX_STEPS=32
TASK_PLAN_CONTEXT_CHARS=2000
TASK_PLAN_MAX_BYTES=65536
```

计划功能默认关闭。关闭时不注册计划工具，也不向模型上下文注入计划摘要。启用后，`tool_names` 缺省并继承完整启用工具 schema 的会话可使用计划工具；显式保存的 `tool_names` 子集不会自动增加计划工具，需将所需工具加入允许集合。启用计划工具只允许维护当前 Agent 私有的计划元数据，不会增加真实文件、命令或委派权限。

配置值必须在以下范围内：步骤数 `1..256`、摘要字符数 `256..16000`、计划文件大小 `1024..1048576` 字节。计划步骤默认上限为 32，摘要默认上限为 2000 字符，单计划文件默认上限为 65536 字节。工具 schema 会占用少量上下文，即使暂时没有活动计划；无计划时不注入计划摘要。

每个 Agent 维护自己的计划。父 Agent 只能绑定并查询自己创建且拥有的子 Agent/后台命令任务，不能读取子 Agent 私有计划。计划文件位于私有会话状态目录，与共享项目工作区分离。当前仍只支持单层委派；计划工具不会自动创建子 Agent 或启动任何任务。

## 计划和步骤

一个会话最多有一份当前计划，默认最多 32 个步骤；`TASK_PLAN_MAX_STEPS` 可配置为 1–256。每个步骤有稳定 `key`、`title`、`acceptance` 验收条件和 `depends_on` 依赖列表，并保存状态、阻塞原因、结果、证据引用、关联执行引用及创建/更新时间。步骤 key 不可重复，依赖必须指向本计划中的步骤，依赖图不能有环。`ready` 根据依赖与状态计算，不能由模型直接设置。新建步骤只提交 `key`、`title`、`acceptance`、`depends_on`，状态由系统初始化为 `pending`。

例如，可先复现问题，再修复并验证；文档提纲依赖现状检查，可以在修复期间独立准备：

```json
[
  {"key":"inspect","title":"检查现状","acceptance":"定位触发条件","depends_on":[]},
  {"key":"reproduce","title":"复现缺陷","acceptance":"记录稳定失败步骤","depends_on":["inspect"]},
  {"key":"implement","title":"实现修复","acceptance":"失败场景通过","depends_on":["reproduce"]},
  {"key":"docs_draft","title":"准备文档提纲","acceptance":"列出行为与限制","depends_on":["inspect"]},
  {"key":"verify","title":"验证修复","acceptance":"相关回归通过","depends_on":["implement"]},
  {"key":"docs_finalize","title":"补齐使用说明","acceptance":"文档与实际行为一致","depends_on":["verify","docs_draft"]}
]
```

此依赖图不会自行运行并行步骤。Agent 仍需逐步执行、记录状态，或显式调用现有受限队列。

步骤状态包括 `pending`、`in_progress`、`waiting`、`blocked`、`completed`、`failed`、`cancelled`、`interrupted`。计划整体状态由步骤派生：无计划为 `none`，归档后为 `archived`；活动计划按步骤汇总显示进度或阻塞状态。`completed`、`failed` 和 `cancelled` 是终态；失败步骤可回到 `pending` 后显式重试，完成步骤不能重开。一个 Agent 同时最多一个前台执行步骤；等待已绑定外部任务的步骤处于 `waiting`，不占用前台步骤。不同分支仍需 Agent 显式调用既有队列工具，计划本身不会并行派发。

每轮模型调用最多注入 `TASK_PLAN_CONTEXT_CHARS` 字符的简要摘要，包含当前目标、前台步骤、阻塞原因、就绪步骤、已绑定任务状态和预算。完整计划通过 `plan_get` 分页读取，不会反复追加到对话历史。摘要是任务数据，不是系统指令、授权或确认。启用工具本身仍会占用少量工具 schema 上下文。若子 Agent 或命令队列尚未初始化，关联状态可从对应持久队列记录只读恢复，并核对当前会话归属；不会因此启动队列任务或额外模型调用。无计划时不注入摘要。

## Agent 工具

工具只能操作当前调用者自己的计划，身份由运行上下文确定；参数不接受任意会话 owner 或路径。所有更新均携带当前会话 revision，避免并发或过期请求覆盖新状态。revision 在同一会话生命周期内单调递增，计划创建和归档也会推进 revision。revision 冲突时先重新读取当前计划，再决定如何提交。

| 工具 | 参数 | 用途 |
|---|---|---|
| `plan_create` | `goal, tasks, expected_revision` | 创建计划。首次传入 `expected_revision=0`；已有计划须先显式归档。创建成功后 revision 递增，步骤初始为 `pending`。 |
| `plan_get` | `cursor, limit` | 分页读取当前计划、派生状态、可引用证据和关联队列状态；`limit` 为 1–256，默认 32。游标绑定计划和 revision，更新后需要重新读取。 |
| `plan_update` | `plan_id, task_key, status, result, block_reason, evidence_refs, expected_revision` | 按合法状态迁移更新步骤；`completed` 必须提供结果和证据引用，解除阻塞/中断后重新校验依赖。 |
| `plan_revise` | `plan_id, add_tasks, edit_pending_tasks, expected_revision` | 追加步骤或编辑尚未开始步骤的标题、验收条件与依赖，并重新校验完整 DAG。不能删除历史步骤或修改已开始步骤。 |
| `plan_bind` | `plan_id, task_key, agent_task_id?, job_id?, expected_revision` | `agent_task_id` 与 `job_id` 必须且只能填写一个；只绑定当前调用者拥有的真实子 Agent 或后台命令记录。绑定后步骤为 `waiting`；队列运行态只读投影。 |
| `plan_archive` | `plan_id, expected_revision` | 归档已结束且关联执行已排空的计划；不取消工作、不删除历史。 |

`plan_bind` 每次只接受一种执行 ID（`agent_task_id` 或 `job_id`）。队列完成且真实 worker 已退出后，关联执行显示为 `ready_for_review`，Agent 需要读取结果并检查证据后再更新计划；队列状态不会自动把计划步骤标成 `completed`。取消也必须确认关联执行确实停止并排空，队列记录提前显示取消不能代替该检查。

工具响应含 `plan_id`、单调递增的会话 `revision`、计划 `status`、步骤 `counts`、当前页 `tasks`、派生 `ready` 列表、`verification`、`available_evidence`、`total_tasks` 和 `next_cursor`。证据条目提供 `id`、`source`、`tool_call_id`、工具 `name` 和结果 `summary`。任何计划写入（包括工具证据追加）都会推进会话 revision；旧游标和旧写入请求需要重新读取。归档后当前 `plan_id` 为空，`archived_plan_id` 保留已归档计划的标识；普通 `plan_get` 不返回归档历史，也不会被新计划覆盖。

写工具为模型返回精简确认：`plan_id`、`revision`、`status`、`verification`、`ready`、`counts` 和 `affected_step`。`plan_get` 的响应受 `TOOL_MAX_OUTPUT` 限制，会在保持合法 JSON 的前提下缩小整页；长字段会列入 `truncated_fields`，近期证据摘要不完整时会设置 `details_truncated` 并提供 `evidence_total`。截断后的步骤或依赖列表不能当作完整计划；需要完整内容时用 CLI `/plan` 分页或 Web 看板的只读 GET 查看私有计划。

Web 看板 GET 与 SSE 摘要使用 `execution_signature` 标识已绑定队列的真实状态、排空情况和运行状态变化；收到 revision 或 signature 变化后自动刷新。后台命令队列没有 SSE 通知时，看板仅在打开期间每 8 秒轮询一次。归档快照若已保存但当前文件的归档标记写入失败，会冻结该计划的证据索引以避免继续变更快照；普通会话工具消息原文仍保留，不会因归档失败删除。

## 完成、证据和恢复

`completed` 表示 Agent 自报步骤完成，不表示系统已验证验收条件。MVP 中完成项显示为 `unverified`，不能误读成验收通过。证据引用由已记录的真实工具调用及配对结果提供；失败结果或恢复时合成的“结果未知”记录不能证明操作成功。系统验证证据身份、来源与调用结果配对，但不解析自由文本来判断业务正确性。

停止、错误、预算检查点或重启时，尚未结束的前台步骤会转为 `interrupted`，而不是伪报完成。队列执行状态独立显示；结果未知时先检查子会话或工作区，再显式从 `pending` 重新开始，不自动重放。会话运行状态 `idle` 也不表示计划已经完成；Agent 未汇报的步骤需要检查。

CLI 输入 `/plan` 可只读查看当前会话的计划、依赖、进度及阻塞原因。WebUI 在运行状态之外提供单独的 `/plan` 看板，通过 `GET /api/sessions/{identifier}/plan?cursor=&limit=32` 只读获取当前计划；已有会话状态 SSE 提供轻量计划摘要，详情分页仍从 GET 获取。两种界面均不会从看板触发计划更新或启动执行。

更多数据模型、崩溃一致性和验证设计见[任务状态与计划设计](task-state-design.md)。
