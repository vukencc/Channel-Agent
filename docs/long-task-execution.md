# 长任务执行与阶段续跑

长任务默认不受固定模型轮数上限限制：`MAX_TOOL_ROUNDS=0` 表示不设轮数上限，根会话与子 Agent 均使用此行为。正数仍设置明确上限。旧配置 `AUTO_CONTINUE_TASKS`、`TASK_MAX_CONTINUATIONS` 仅在仍使用正数阶段上限的会话中保留兼容续阶段控制；默认 0 轮策略不按续阶段次数停止。连续执行仍受取消、超时、费用/请求预算、无执行进展（连续 16 轮）等保护。执行沿用同一持久会话和已保存状态，不创建新的用户消息、不伪造工具结果，也不重放已执行工具调用。

## 配置与停止条件

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `MAX_TOOL_ROUNDS` | `0` | 0 不设固定模型轮数上限；正数为上限。显式会话正数覆盖保持有效。 |
| `AUTO_CONTINUE_TASKS` | `false` | 兼容旧正数轮数策略的根会话续阶段开关；不限制默认 0 轮策略。 |
| `TASK_MAX_CONTINUATIONS` | `3` | 旧正数轮数策略下的续阶段兼容上限，范围 1–8；不含初始阶段。默认 0 轮策略不在此计数处停止。 |

每轮继续依赖实际执行进展。被拒绝、取消、超时或失败的调用不算成功工具进展；反复无工具进展达到 16 轮时保存 `no_execution_progress` 检查点并停止。计划已阻塞或等待，且没有活动或待验收的子 Agent/后台命令可继续处理时，保存 `plan_waiting` 检查点并请求用户提供信息或解除阻塞。仍在运行的执行，以及已结束但尚待 Agent 审查的绑定执行，保持可继续处理状态。有限正数轮数上限达到时保存 `tool_budget` 检查点。检查点保留已完成的工具结果和计划状态，之后用户可在同一会话明确继续。

阶段续跑不会突破成本或速率预算、模型恢复次数、工具超时或单轮工具调用限制。显式轮数覆盖和子 Agent 的 `max_rounds` 仍是各自硬上限；自动续跑不会扩大子 Agent 权限或工具集。

## 计划与验收

复杂工作适合用任务计划记录目标、步骤、依赖和验收条件。阶段边界用于保存可检查的进度，不代表整个目标已完成。继续前应查看当前计划与真实工具结果，复用已有产物，只执行缺少的步骤；每阶段结束后核对结果和验收证据，再决定继续、等待用户还是结束。任务计划的工具和状态定义见[任务计划使用说明](task-plans.md)。

如果工具集中有 `create_session` 和 `wait_agent_task`，运行指导会建议在复杂且可独立验收的任务中考虑并行委派。它建议最多启动两个独立子任务，同时由根 Agent 推进自己的工作；这是适用时的建议，不要求每个任务委派，也不会自动增加工具权限。子任务需有明确的文件所有权、验收条件和返回证据，避免多个写入者修改同一文件。子 Agent 的确认、权限和共享工作区边界仍由现有会话规则决定。

`wait_agent_task(task_id, timeout_seconds=10)` 只等待当前会话自己创建的子任务，不需要额外确认。默认等待 10 秒，参数范围 0–30 秒，实际等待会收窄到通用工具超时以内；等待工具不占用读工具并发槽。它返回 JSON 状态、`drained`、有界的 `result`/`error` 及截断标记。它同时检查子会话运行状态和在途工具是否已结束；等待超时不取消任务，也不会启动或重放任务。根据状态和 `drained` 判断是否结束，然后读取并验证子任务产物及相关测试。不要用循环状态查询替代一次有界等待；超时后可继续自己的工作，再择机等待。

没有可行子任务时，直接完成当前工作。计划处于 blocked/waiting 状态且没有活动子任务时，应保存检查点并向用户说明需要的信息或决策，不应反复重启阶段。

## 执行记录与验证

历史运行曾使用显式正数阶段轮数上限：一次记录在 24 次成功模型请求后以 `tool_budget` 检查点结束，期间共有 51 次工具调用，总运行时间 486.858 秒。计划的第一阶段为 `interrupted`，仍有 4 项 `pending`，且没有创建子 Agent。该旧记录说明阶段检查点会保留未完成计划与已执行结果，不代表默认配置或任务验收通过。会话或运行标识已省略。

## 改动与回归

| 问题 | 实现位置 | 验证重点 |
| --- | --- | --- |
| 无固定轮数执行 | `src/agent_core/core/task_execution.py`、`src/agent_core/core/sessions.py`、`src/agent_core/config.py` | 默认无限、正数轮数限制、16 轮无进展检查、取消/预算/超时停止 |
| 计划与执行等待 | `src/agent_core/core/task_plans.py`、`src/agent_core/core/task_execution.py` | 子 Agent 与后台命令排队、运行、已完成待验收状态；不会自动伪造验收 |
| 子任务等待阻挡读取 | `src/agent_core/core/agent_tasks.py`、`src/agent_core/tools/session_tools.py`、`src/agent_core/tools/base.py` | 等待不占读槽，超时/取消不重放子任务，线程排空后才报告 drained |
| 委派提示与轮数隔离 | `src/agent_core/core/delegation_guidance.py`、`src/agent_core/core/tool_settings.py` | 根据实际工具集提示独立分工；每个会话使用各自有效轮数 |
| Compact 条件过宽 | `src/agent_core/core/history_context.py` | 高占用且有效高方差或硬预算不足；相同原文不重复软压缩，完整原文与配对不变 |
| 上下文状态显示 | `src/agent_frontend/web/context_status.py`、`src/agent_frontend/web/static/app.js` | 准备、监督检查、实际压缩阶段与安全白名单指标 |

回归覆盖位于 `src/agent_test/tests/test_task_execution_policy.py`、`src/agent_test/tests/test_agent_task_wait.py`、`src/agent_test/tests/test_compact_trigger_policy.py`、`src/agent_test/tests/test_session_tool_schema.py` 和 `src/agent_test/tests/test_plan_recovery_guidance.py`；没有新增模型或 Python 依赖。

运行配置可显式设置正数 `MAX_TOOL_ROUNDS` 保留旧式分段上限；默认 0 不设置轮数终点。自动继续与等待均写入会话审计；等待不需要额外确认，创建子任务沿用原有确认。子 Agent 仍不能继续创建子 Agent。

## 最终验证

2026-10-08，Python 3.12.14 / WSL Ubuntu：

- uv sync --locked 通过；另用既有 web extra 安装并验证 WebUI。
- 测试启动隔离本机配置后的全量回归：903 passed、2 skipped、5 deselected，26.75 秒。2 项跳过要求可选 ann 依赖；5 项真实 RAG integration 未运行。
- Bubblewrap + prlimit 实际隔离执行通过，没有退回主机执行。
- 新策略显式开启测试、真实临时文件读写、后台隔离命令、两个子任务重叠调度、SDK 请求 schema 并发隔离均通过。模型外部调用在单元测试中用桩替代，不把这些调度测试当成真实模型决策质量。
- Node 执行了 WebUI 交互状态与布局行为检查；Node 通过临时 WSL 包装调用本机 Windows Node，不新增仓库依赖。
- git diff --check 通过；保留本轮开始前已有的未提交工作，没有向真实用户 .agent/、crud_tests/、data/raw/、logs/、.cache/ 写入测试内容。

曾直接读取本机 .env 跑全量，得到 18 failed、885 passed、2 skipped、5 deselected；失败包括已开启结构化上下文导致旧模型桩收到带标题的用户输入，以及导入时注册计划工具后“功能关闭”用例无法清除注册。这不是并发负载的推测。随后仅隔离测试启动的三个功能开关，使默认用例对应模板默认值；结构化上下文、计划与续跑测试仍由各自夹具显式开启，保留原断言。应用 .env 不受测试隔离影响。

复现完整回归（测试启动时使用默认关闭功能作为统一基线，各功能测试通过夹具显式开启）：

    uv run --locked --extra web pytest -m 'not integration' -q -rs
    uv run --locked --extra web python -m agent_test.scripts.ci_sandbox_probe

本机测试审计写临时目录；前端检查需要 PATH 中可用的 Node。

## 触发路径实测

固定合成增长轨迹，每种模式独立执行 3 次、每次 36 轮，compact ratio=0.9。调用真实 HistoryContext.prepare 和临时持久化，监督评分及摘要使用确定性桩；没有任何 LLM 请求或真实项目工具调用，不是语义效果或端到端吞吐评测。

| 监督模式 | Compact 次数/36 轮（前→后） | 评分次数（前→后） | 准备总耗时中位数秒（前→后） | 新触发原因 |
| --- | --- | --- | --- | --- |
| disabled | 5→5 | 0→0 | 0.05175→0.03422 | context_insufficient |
| uniform，方差 0 | 5→5 | 5→10 | 0.05250→0.03474 | context_insufficient |
| mixed，方差 1 | 5→5 | 5→5 | 0.05131→0.03525 | high_utilization_and_variance |

每次运行摘要桩调用均为 30 次：25 次 chunk、5 次 session；未走摘录降级。修复前均按 budget_risk 触发。持续增长最终仍会耗尽预算，因此总压缩次数没有下降；低方差窗口在软阈值时保留原文，到预算不足才压缩。uniform 评分次数增加，可能增加真实监督服务成本。毫秒级本地耗时受系统负载及文件缓存影响，不能据此声称模型或整个任务提速。

命令（MODE 为 disabled/uniform/mixed；前后 JSON 均保留在本机临时目录）：

    .venv/bin/python -m agent_test.scripts.benchmark_context_progress --runs 3 --rounds 36 --compact-ratio 0.9 --supervisor-mode MODE --observations --output /tmp/context-policy-final-observed-MODE-20261008.json

## 在线模型试验

第一次试验在全新临时目录建立纯标准库双模块项目，真实模型和真实工具执行；协调者拥有文本模块与整体验证，尝试把数值模块交给子 Agent。设置每阶段 4 轮、3 次续阶段，总上限 16，保持工具与验收门禁。

实测 16 次模型请求、3 次自动续阶段、27 次工具调用（22 次 executed、5 次 execution_error）；实际 python3 -m unittest 输出 Ran 14 tests 和 OK。但最终 plan 仍有 4 项 pending，验收为 unverified，运行以 task_continuation_limit 保存检查点。因此这是产物测试通过，不能声明整体验收完成，也不能声称本次验证了并行委派。

失败证据：create_session 请求 8 轮，超过父会话有效上限 4；另有计划完成缺少真实证据引用、非法状态迁移和旧 revision。工具与预算门禁正确拒绝。随后修复请求 schema 未显示当前上限的问题，并补充 completed 的 result/evidence_refs 字段说明：引用 plan_get.available_evidence.id 且 source=executed，不使用 tool_call_id。并发 schema、超额拒绝和证据说明回归先失败后通过，未放宽后端约束。

第二次隔离在线试验使用每阶段 8 轮、最多 32 轮，约 255 秒结束，没有实验超时。根 Agent 发起 49 次工具调用（45 次 executed、4 次 execution_error），使用 3 次自动续阶段；实际 python3 -m unittest 最终输出 Ran 18 tests 和 OK。并发峰值为 2 个活动 Agent、2 个模型请求、1 个工具执行。创建的子任务队列状态为 completed，但它只有 1 次模型请求、没有真实工具调用，所写工具调用草稿不是执行证据；产物由根 Agent 完成。

该试验仍以 task_continuation_limit 保存检查点，计划 revision=8、2 completed、1 waiting、3 pending，verification=unverified。四个错误分别为 waiting→failed 和三次 pending→completed；现有状态机正确拒绝。这暴露了摘要遗漏已排空 waiting 步骤的恢复指导，以及非法迁移错误信息无法指导模型纠正的问题。后续修复保持状态机、证据、CAS 与排空约束，明确先恢复到 in_progress，再验收或失败；不把队列完成视为产物完成。

第三次沿用同一项目任务，使用默认每阶段 24 轮、最多 96 轮，实验总时限 450 秒。最终没有实验超时，根会话 idle、没有停止原因，36 次模型请求、1 次自动续阶段，计划 revision=18、全部 7 项 completed，每项均附执行证据。根会话 48 次工具调用（46 次 executed、2 次 execution_error）；子 Agent 7 次模型请求、7 次实际工具执行后结束。两者合计 55 次工具调用、53 次 executed。实际 python3 -m unittest 输出 Ran 13 tests in 0.000s 和 OK；文件包括两个模块、各自单元测试、整合测试与 README。

观察到同时活动的 Agent 和模型请求均达到 2，工具调用在途峰值为 5。工具指标统计进入工具调度的调用，包含等待或排队，不能等同为 5 个原生命令同时运行。读工具槽、工作区写锁、写操作确认和实际沙箱仍按原规则执行。仍有两次重复 completed 请求被状态机拒绝，模型随后纠正，未妨碍计划结束。

计划 verification 保持 unverified，这是现有人工验收标识，系统不会把 Agent 的 completed 自报转换为人工确认；任务运行结束、步骤完成、实际测试通过均分别记录。上述为一次隔离的标准库工程试验，测试由运行中的 Agent 编写，不能据此保证任意大型项目都会成功，或证明语义质量提升。前两次失败报告保留，未作为通过结果统计。

本机原始 JSON 位于 /tmp/long-task-live-validation.json、/tmp/long-task-live-validation-after.json、/tmp/long-task-live-validation-final.json；三个试验使用独立临时状态、项目工作区和审计路径。真实用户长任务未被重启、重放或改写。没有引入新模型、依赖或强制自动委派；网络服务超时仍按已有有限重试策略处理。
