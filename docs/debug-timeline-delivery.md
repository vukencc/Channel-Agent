> 历史报告，不作为当前使用规范；保留当时的问题描述和实测结果。当前说明见[当前调试与上下文指南](debug-runtime.md)。

# DEBUG 时间线交付报告

验收日期：2026-10-15。已完成实现、回归测试、真实工具执行与浏览器验收。

## 使用方法

在本地 `.env` 中设置 `DEBUG=true`，重启 WebUI，打开 `/debug`。开启后才采集事件；不增加模型请求。默认保留最近 512 条时间线事件，可通过现有 `DEBUG_TRACE_MAX_EVENTS` 调整，范围为 1–10000。

## 完成功能与实现

| 功能 | 实现方式与主要文件 |
| --- | --- |
| 主子 Agent 分支 | `core/agent_tasks.py` 在实际创建、启动和完成时记录父子关系；`web/static/debug-view.js` 在共同时间轴上计算各 Agent 轨道与分支起点。颜色在轨道、事件和曲线间保持一致。 |
| 通信连线 | `core/communication.py` 用发送者、接收者、消息序号关联真实发送与读取。未读为虚线，已读为实线，方向箭头表达收发关系。 |
| 工具折叠与展开 | `core/sessions.py` 为每轮调用设置批次及工具标识；页面默认折叠同批次工具，展开后呈现各自的实际起止时间和并发重叠。短调用的文字标签与真实耗时条分开，便于读取。 |
| 阶段与结果 | 上方显示阶段切换、计划步骤和状态、最终回复、停止等事件；下方显示工具行为。模块耗时可单独开启叠加，默认关闭。 |
| 上下文与 token 曲线 | 同一时间轴、独立纵轴，按 Agent 区分；显示 Compact 前后占用、每次请求或当前窗口累计的输入/输出 token。服务商 usage、本地估算和未知值分别标记；未知值留空。 |
| 交互与恢复 | 支持缩放、拖动、跟随最新、按 Agent/异常筛选、暂停显示与恢复补齐。暂停仅冻结画面，不暂停任务。进程重启后自动清除旧代事件与详情。 |
| 按需详情 | `web/debug_details.py` 从原消息定位最终回复、工具参数和配对结果，显式点击才读取；返回前脱敏并分页，不包含 reasoning。普通快照及 JSON 导出仅含元数据。 |

`core/debug_runtime.py` 提供线程安全序号、单调时间、有限容量、进程代号及增量游标；`web/app.py` 提供增量快照和详情接口。容量淘汰或游标失效会通知页面重建窗口。既有模块面板和上下文归档入口保留。

## 验收中发现并修复的问题

- 跨轮复用工具 ID 会关联到错误调用：使用轮次和工具 ID 联合定位，并保留来源消息位置。
- 并发发送时，接收端可能先记录“已读”、发送端后记录“发送”：将发送事件放到邮箱锁内、原子持久化成功之后，保证实际可见顺序。
- 详情中的 JSON 凭据字段可能漏脱敏：统一识别完整密钥字段，覆盖字符串、嵌套对象和数组，并在分页前处理；新增失败复现测试。
- 高频通信匹配逐条搜索导致计算增长过快：改成一次建立消息索引，保留因果校验。
- 活动调用计时停住、Agent 颜色碰撞、毫秒级工具文字不可读：分别使用持续推进的快照时钟、独立颜色分配、标签与耗时条分离处理。

## 首轮实现测试与结果

环境：WSL Ubuntu、Python 3.12.14、Node v24.21.0。使用 Linux 环境运行，未使用 Windows 可执行文件修改共享虚拟环境。

| 验证 | 结果 |
| --- | --- |
| `uv sync --locked` | 通过 |
| `uv sync --locked --extra web` | 通过；安装现有 Web 可选依赖 |
| `UV_NO_SYNC=1 uv run pytest -m 'not integration' -q -rs` | **1123 passed、2 skipped、5 deselected，38.85 秒** |
| `UV_NO_SYNC=1 uv run python dev/ci_sandbox_probe.py` | Bubblewrap + prlimit 实际隔离执行通过 |
| `git diff --check` | 通过 |

两项跳过来自未安装可选 `hnswlib` 的 ANN 测试；五项真实模型集成测试未运行，不能视为通过。新增 `tests/test_debug_timeline_*.py` 覆盖记录器、页面视图、API、执行链路、Compact/计划事件、详情脱敏、并发通信。保留既有测试，修复前已复现相关失败。

浏览器通过 `scripts/debug_timeline_validation.py` 启动临时实例：模型边界使用受控测试适配器，父子 Agent 调度、文件读取、沙箱命令和邮箱收发均实际执行。token 标记为本地估算，未冒充服务商统计，也未调用收费模型。测试状态位于临时目录，未读写用户会话或知识库数据。

- 1440 px 桌面与 450 px 窄屏正常；窄屏工具栏无外部横向溢出，时间线内部可横向浏览。
- 验证主子轨道、通信、工具展开与真实结果详情；暂停时画面停留在 133 个事件，后台继续到 197 个事件，恢复后补齐。
- 验证缩放及重启后的代号切换与旧详情清理；最终浏览器控制台 **0 error、0 warning**。
- 截图：[桌面](../.cache/reports/debug-timeline-desktop-133258.png)、[窄屏](../.cache/reports/debug-timeline-mobile-133258.png)。

## 性能数据

同一 Node 环境、同一组四 Agent 元数据输入，构造数据不计入测量；每组 9 轮，记录中位数及最近秩 p95，执行实际 `debug-view.js`。

| 场景 | 优化前中位数 / p95 | 优化后中位数 / p95 |
| --- | --- | --- |
| 10000 条通信事件，5000 对收发 | 45.71 / 163.57 ms | **7.36 / 11.15 ms** |
| 512 条通信事件 | 0.631 / 1.604 ms | 0.299 / 1.260 ms |
| 10000 条普通事件 | 1.836 / 2.574 ms | 1.765 / 2.461 ms |

通信场景中位耗时降低约 83.9%。这是视图数据计算基准，**不是 DOM 绘制、浏览器帧率或整个模型任务的耗时**。原始数据：[优化前](../.cache/reports/debug-timeline-perf-6c03dfcdccda4fcda06662aa187b26ea.json)、[优化后](../.cache/reports/debug-timeline-perf-867a0954c09e4595963bdb87b024f63b.json)。

## 概览密度更新（2026-10-15）

旧概览为每个阶段碰撞轨迹和每组工具都生成单独行；包含 500 个工具批次的合成布局用例因此增长到 20,412 px。现在每个 Agent 的默认轨道固定为 150 px；阶段条按约 14 px、关键节点按约 112 px、工具区域按约 148 px 的屏幕密度聚合。密集且共址的通信在概览合并，实际消息成员仍保留。点击阶段、工具或消息后使用每页 20 行的列表逐层定位；同一时间只展开一个工具区域，调用预览最多 6 条，其余可分页读取。原始 JSON 元数据默认折叠。建议操作顺序：整体概览 → 点击阶段/工具/消息 → 翻页定位 → 选中具体记录 → 按需读取详情。

固定高度验证：500 个工具批次从 20,412 px 降至 150 px；2500 个从 101,832 px 降至 150 px。仅聚合展示，不删减源事件或改变真实工具区间。另修复了运行中的局部详情不更新、同批次其他工具失败后摘要仍报零异常、筛选 Agent 导致跨 Agent 消息详情消失，以及等待确认漏计时、跨轮空闲被算作阶段持续时间的问题。已加载的详情在其他调用更新时保留。

浏览器 fixture 通过 `uv run python scripts/debug_timeline_validation.py --rounds 20` 启动，脚本使用受控模型回调，会实际执行工具、子任务和通信，监督器外部连接在脚本进程中显式关闭。参数范围为 1–100，状态使用临时目录。本轮 20 轮执行产生 2742 条事件、40 条父子消息，两个 Agent 的默认场景总高为 300 px。验证了聚合展开、28 次调用的两页列表、具体调用真实结果、Agent 筛选后的双端消息、收起恢复概览；450 px 窄屏无页面横向溢出，控制台 0 error / 0 warning。临时服务已关闭。

截图：[整体概览](../.cache/reports/debug-overview-desktop-165156.png)、[局部详情](../.cache/reports/debug-overview-details-165156.png)、[窄屏](../.cache/reports/debug-overview-mobile-165156.png)。

概览计算微基准在相同 Node v24.21.0 fixture 下执行 9 轮、预热 2 轮：

| 输入规模 | 优化前中位数 / p95 | 优化后中位数 / p95 |
| --- | --- | --- |
| 500 个工具批次 | 2.362 / 2.954 ms | 2.602 / 3.382 ms |
| 2500 个工具批次 | 26.102 / 33.054 ms | 11.023 / 13.343 ms |

小输入的测量略慢，大输入改善；这些数据仅描述该用例下的视图计算，不代表浏览器 FPS、DOM 绘制或所有规模都更快。可复现命令：`node .cache/reports/debug-timeline-overview-bench.cjs before` 与 `after`。脚本保留旧布局算法，报告含源文件指纹；记录见 [优化前](../.cache/reports/debug-timeline-overview-before.json) 与 [优化后](../.cache/reports/debug-timeline-overview-after.json)。

新增 `tests/test_debug_timeline_overview.py` 的 **9 项测试通过**，覆盖密集事件布局、完整成员、分支/通信/指标、分页、实时更新、确认计时和空闲边界；修复前已运行失败复现。最终验证：

- `uv sync --locked`、`uv sync --locked --extra web`：通过，无依赖或锁文件变更。
- `UV_NO_SYNC=1 uv run pytest tests/test_debug_timeline_overview.py tests/test_debug_timeline_view.py tests/test_web_context_navigation.py -q`：**34 passed**。
- `UV_NO_SYNC=1 uv run pytest -m 'not integration' -q -rs`：**1132 passed、2 skipped、5 deselected，42.03 秒**。跳过/未运行原因同首轮记录。
- `UV_NO_SYNC=1 uv run python dev/ci_sandbox_probe.py`：实际隔离执行通过；`git diff --check`：通过。保留既有安全语义与全部原测试。

## 配置、隐私与边界

- 本轮没有新增配置项、依赖或模型；`.env.example` 补充现有 DEBUG 设置的说明。
- 普通追踪仅在进程内保留有限元数据，重启不恢复；上下文归档仍使用原有会话存储。时间线不会额外落盘消息正文。
- 详情最多处理 1 MiB，每页最多 6000 个 Unicode 字符；先脱敏后分页。凭据字段与已知密钥被隐藏，其他工作内容仍应按本地调试资料管理。
- token 累计仅覆盖当前保留窗口中已知的采样，不能当作完整账单；上下文占用是包含预留空间的预算估值。
- 保持现有沙箱、确认、审计、访问边界、工具结果配对及 Compact 触发规则；DEBUG 仅观察执行，不改变调度或主动触发整理。
