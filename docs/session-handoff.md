# 新会话交接说明

> 文档类型：当前维护交接 · [文档导航](README.md) · [历史交接](session-history.md)

截至 2026-11-03，阶段文档已整理，全部既有代码、测试与文档已提交并推送，已发起 [PR #35](https://github.com/vukencc/Channel-Agent/pull/35)。先阅读根 [AGENTS.md](../AGENTS.md)，检查 Git 状态并保留全部已有改动；使用入口见 [快速上手](getting-started.md)，功能与限制见 [阶段状态](project-status.md)，按主题查 [文档索引](README.md)。

## 当前工作与 Git

- 2026-11-04，Web 和桌面 Flow 节点支持「复用会话」，默认关闭。项目节点保存会话绑定，并将新输入与本轮交接追加到同一会话。忙碌时按整轮 Flow 排队，停止、失败或重启后暂停等待项。绑定、队列和输入不会进入便携模板。专项 94 项通过，非集成回归 1846 项通过、2 项跳过、5 项未选中，实际 Bubblewrap 和 prlimit 探针通过。Chrome 和 Qt offscreen 均用独立状态及受控模型回调走通两轮，验证多行输入、排队、会话 ID 和下游交接。Chrome 另验证 416px 窄窗。用法见 [Agent Flow](agent-flow.md)。
- 2026-11-04，PR #35 已合并到 `main`（`432aeeb`）。随后在本地调整 WebUI 左栏，将新建项目移到顶部，并为会话、Flow 项目和 Flow 模板设置独立分区及统一滚动。实际浏览器验证了 190px 侧栏与约 416px 页面下的布局，以及新建、选择项目和模板、打开对话设计。验证使用独立状态和合成条目。专项 77 项通过，非集成回归 1835 项通过、2 项跳过、5 项未选中。
- 旧 PR #29 已合并。其后本地积累的 Flow 项目隔离、画布、创作与上下文修复均属于本次提交范围；排除凭据、用户状态和生成产物。
- 已将旧分支安全快进到 `origin/main` 的 `032c062`，只更新上游 `uv.lock`，完整保留未提交改动；本次分支为 `codex/flow-milestone-docs`。
- 文档整理新增入门与阶段总结，并将旧交接移到 [历史会话交接](session-history.md)。历史报告保留失败和验证边界，当前指南优先。
- 功能代码、用例和回归提交为 `f2f5419`，阶段文档为 `32014ac`。PR #35 目标为 `main`，交付代码 `027dc15` 的 [CI 回归](https://github.com/vukencc/Channel-Agent/actions/runs/41663576826) 为 1830 passed、7 skipped、5 deselected。当前提交的检查与合并状态以 PR 页面为准。

## 实现边界

- 三模块目录为 `src/agent_core/`、`src/agent_test/`、`src/agent_frontend/`，各自也是独立 uv 项目与 Python 包根。Core 不导入 UI/Debug 实现；前端组装时注入中性观察器。详见 [架构](architecture.md)。
- 原生 Qt 界面独立于浏览器；WebUI 提供同一 Core 的经典会话、Flow 画布、设计侧栏、节点工具/模型/思考设置与便携 JSON 导入导出。CLI 提供经典会话。
- Flow 模板可以独立创建，运行须明确绑定项目和有效 Linux 工作区；创建项目可导入模板副本。节点独立会话共用该项目目录，按拓扑串行调度，分支箭头不等于条件路由或并行。所有运行入口须保留项目身份和路径保护，不能回退到应用源码目录。
- 自动 Handoff 传递上游结束正文与项目上下文，属于 Agent 自报信息；节点 completed、工具退出 0 和业务验收通过是不同事实。节点失败、检查点、取消或未排空会阻断下游。
- 经典 Agent 工具由框架管理。Flow 节点可收窄有效工具，`read_file`/`list_files` 基础锁定，部署及祖先能力上限同时约束 schema 和执行。提示词不构成文件权限隔离。
- 设计助手没有执行工具，只创建新的未绑定模板，不覆盖当前图或运行节点。便携 JSON 不携带项目目录、运行身份、连接凭据或知识库；未知模型仅经显式继承选择清空引用。旧测试配方 `schema_version` 格式不能当作 `format=agent-flow/version=1` 的便携文件直接导入。
- Flow 的 local 命令通过 Bubblewrap 限制持久写入到项目，保留网络与 localhost，隔离不可用时拒绝；经典会话 local 语义不同。所有模式继续遵守选定权限、确认、审计和工具结果配对。
- `prepare_context` 是模型调用前准备输入窗口，不代表已 Compact。结构化归档须满足独立的软/硬预算条件；无 `history_read` 时最新未交付完整工具批次受保护，不能容纳时在付费摘要和归档发布前进入检查点，不扩大预算或拆分工具配对。详见 [上下文管理](context-management.md)。
- Debug 保持原上下两行标签与短竖箭头；所有子 Agent 行完整显示，服务端按根会话树隔离。右侧局部详情独立滚动，不强制滚到页面底部；全部 Agent 停止后时间轴冻结。不要擅自重设计主图。

## 验证事实与复用

最近一轮功能交付记录为非集成回归 **1823 passed、2 skipped、5 deselected（135.63 秒）**，真实 Bubblewrap/prlimit 通过；Core/AgentTest wheel 共 133 份源码哈希一致。这是此前代码验证；本次在最新锁定依赖上重跑为 **1823 passed、2 skipped、5 deselected（144.34 秒）**，真实隔离探针通过。三个 wheel 已离线构建，源码与资源哈希由本次交付检查核对。

本次检查 68 份 Markdown、413 个本地链接与 16 份 TOML/锁配置；三个 wheel 的 161 个源码/资源哈希匹配。修复可选质量测试遗留的 `scripts.rag.quality` 模块路径，正确模块为 `agent_test.scripts.rag.quality`；先复现旧入口 ModuleNotFoundError，再验证新入口 help 与 4 项离线测试。首次独立缓存在线 build 遇 PyPI TLS EOF，随后复用已有构建依赖离线成功，未更改锁文件或放松校验。所有 2419 个既有用户文件哈希保持，独立只读交付审查未发现问题。

PR 首轮 CI `41658238961` 出现 8 failed、1810 passed、7 skipped、5 deselected（177.22 秒）。6 项为冻结客服夹具的 CRLF/LF 原始哈希差异：现在仅归一 CRLF 后以 Git LF blob 指纹校验，原始报告哈希保留，数据和业务标准不改；7 项新增回归验证等价换行通过、其他字节变更失败。取消回归的固定 0.1 秒等待在受控慢保存下复现失败，现用有界事件同步，补普通/受阻保存两分支及真实排空断言。桌面 8 秒超时在隔离环境连续 10 次未复现，根因仍未知，只加终态与节点诊断和 1 项回归，不改期限或成功断言。三项修订均经只读复核。

首次 CI 修订后禁用 `.env`、隔离全部测试路径的全量回归 **1833 passed、2 skipped、5 deselected（121.01 秒）**；真实隔离探针再通过。客服验收器 40 项、Core Flow 19 项、桌面 Flow 12 项聚焦通过，取消同步另 20 次独立运行合计 80 passed。首轮失败见对应 GitHub Actions 运行。


第二轮 CI `41661036459` 为 1 failed、1827 passed、7 skipped、5 deselected（134.61 秒），仅余桌面 8 秒超时；新增诊断显示 first completed、next running 且两次模型回调已到。受控单核/线程切换实验复现测试等待泵阻碍后台线程推进，改为 `processEvents` 后主动 `time.sleep(.01)`，保留 8 秒期限和原断言；新真实线程回归覆盖，临时线程配置均恢复。该实验不证明 CI 使用了相同线程参数。

同时独立复现生产竞态：运行审计等待期间先发布 completed，迟到的 queued 操作确认使面板倒退，而 window.state 仍 completed。桌面桥接在 run/cancel 审计后重读权威快照，受控真实 Core/Bridge 回归先失败后通过；此修复不能作为 window.state 超时的解释。最终新增 12 项回归，隔离完整回归 **1835 passed、2 skipped、5 deselected（110.73 秒）**，相关 Qt offscreen **14 passed**，Frontend wheel 已重建。只读复核无发现；本次无可见窗口/窄屏交互验证。当前工程验证以本段为准，远端最新结果查看 PR 检查。

- [设计助手与客服实测](agent-flow-designer-customer-validation-2026-11-03.md)：五轮真实 Flow 中第 05 轮全部节点 completed，但原业务静态验收 562 通过、1 失败，语义另发现人工尝试增造。后续服务→人工定向执行和人工反馈修订后，14 服务/6 人工语义通过；没有新版五节点全量鲜跑，不能将旧失败追认为通过。
- [创作功能验证](agent-flow-authoring-validation-2026-11-02.md)：区分真实 provider 设计、受控模型界面与实际 Chromium/WSLg 检查。
- [设备借用验证](agent-flow-equipment-validation-2026-10-31.md)与 [画布验证](agent-flow-canvas-validation-2026-10-31.md)：保存多节点任务、业务断言及浏览器检查边界。早期 [客服实测](agent-flow-customer-service-validation-2026-11-02.md) 的业务缺陷仍保留。
- 2026-11-04，本机便携客服模板已单独保存到 `/home/elaine_vuken/projects/customer-feedback-flow.template.json`，供导入复用。应用里也保留原五节点客服反馈分流模板。导入后需选择项目和模型，准备反馈文件及 `support` 知识库。历史报告中的缓存路径记录产物的生成位置，复用模板时使用单独保存的 JSON。

2026-11-04 的交接更新触发了 [CI 41751535114](https://github.com/vukencc/Channel-Agent/actions/runs/41751535114)。桌面确认绑定回归在等待终态时超时，其余 1829 项通过。桌面会话与 Flow 生命周期测试的轮询改用已验证的 Qt 事件处理加 Python 线程让出方式，原期限、绑定校验与拒绝后文件状态断言均保留。后续检查见 PR。

本次交付使用离线工程回归，真实模型业务验证沿用上述历史记录。

## 本机开发环境

实际运行在 Linux/WSL，仓库为 `/home/elaine_vuken/projects/ai-agent-startup`。宿主可能显示为 `\\wsl$\Ubuntu\home\elaine_vuken\projects\ai-agent-startup`；所有 uv、Python 和 Node 操作都须通过 WSL，不能用 Windows uv 重建 `.venv`。

- uv：`/home/elaine_vuken/.local/bin/uv`；Python：项目 `.venv/bin/python`。
- Node：`/home/elaine_vuken/.nvm/versions/node/v24.21.0/bin/node`，非登录 shell 需将此目录加入 PATH。不要仅查 Windows PATH 就宣布 Node 不存在。
- 根目录执行 `uv sync --locked --extra web --extra desktop`，随后 `uv run --no-sync ...` 保留已安装的可选依赖；`.env` 只在不存在时从样例创建。
- 非集成回归显式设置 `COMMAND_EXECUTION_ENVIRONMENT=sandbox` 与 `QT_QPA_PLATFORM=offscreen`；工具变化还需真实 `python -m agent_test.scripts.ci_sandbox_probe`。实际界面检查、真实模型与受控 fixture 必须分别记录。
- 此次 WSL Git 推送挂起，宿主 Git 原代理 `127.0.0.1:7892` 不可用；最终通过宿主 Git 的既有认证，以单次 `-c http.proxy=http://127.0.0.1:7890` 和 HTTP/1.1 完成推送。只对该仓库设置单次 `safe.directory`，没有更改全局 Git、系统代理或用户凭据。

## 数据与协作约束

新验证产物放在唯一的 `.cache/reports/` 子目录。2026-11-04 用户授权交付后清理多余的测试报告、截图、缓存和临时项目。根据验收记录识别生成目录，区分用户的 `.agent/` 会话、`crud_tests/` 工作区、原始数据、应用日志和本机凭据。源码、回归用例、历史 Markdown 说明与 GitHub CI 记录继续作为维护依据。

根 `AGENTS.md` 和 `.codex/` 的默认配置是已跟踪文件，随 clone 分发；`docs/dev/` 和被忽略的本地日志/缓存不分发。模型及推理配置仅在 `.codex/agents/*.toml` 维护；仓库开发子代理与应用内子 Agent 是不同系统。协作约定见 [Codex 多代理](codex-multiagent.md)，历史状态参见 [历史交接](session-history.md)。
