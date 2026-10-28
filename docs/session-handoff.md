# 新会话交接说明

> 2026-10-28 Web Flow 对等与多用例验收：WebUI 已接入同一 Core FlowService，提供 Agent/Flow 项目创建、经典初始权限、Flow 列表、拖动节点/连线、缩放适配、Flow/节点/运行检查器、可视化服务器 Linux 项目选择、可选背景、保存/运行/取消/删除和节点会话跳转。默认项目与继承说明、自动交接语义与原生一致；SSE 仅传轻概要、flows_revision 和生命周期 flows_epoch，完整图按需读取。Core 与工具权限实现未改，Web 适配补齐模型引用、项目预约、记忆与维护保护。
>
> 多用例实际发现并修复 pending 吞最终状态、服务重启版本归零、长 run 回传触发 413、目录写旧草稿、名称后连续输入丢指令，以及路径/负坐标/tab 和经典初始权限对等问题。新增回归 34 项，与既有 Web/Core 聚焦 **67 passed**；最终完整非集成回归 **1626 passed、2 skipped、5 deselected（95.14 秒）**，跳过为可选 ANN extra。locked sync/lock check、真实沙箱探针和 Frontend wheel 新静态资源/桌面选择器检查通过，最终只读增量复核无确认发现。
>
> 真实浏览器在独立服务验证目录选择、串行、分支汇合、取消、失败、运行锁、草稿回切、会话跳转、删除保留记录、450 px 无横向溢出，以及运行中实际中断服务后页面不刷新恢复、旧 run interrupted 且不自动重放；模型为受控回调。另执行 **33 次真实 provider 调用**：sandbox 串行修改和独立校验通过；初次 local 分支因临时验收驱动共享额度耗尽而停止，保留失败且下游阻断，不计为成功；独立 local/readonly 汇合完成且项目文件不变。没有给生产添加该驱动额度。完整问题、证据类型和边界见[多用例验收](agent-flow-validation.md)，脱敏摘要/截图在 `.cache/reports/web-flow-20261028-8ssjcb04/`。
>
> 用户已明确选择恢复并更新 README，本轮已落实，取代前条保留删除的本机状态。沿用 `codex/live-project-validation` 和 PR #29；用户本轮授权提交、推送全部项目改动并在检查通过后合并，最终提交、CI 与合并状态以 Git/PR 为准。独立测试项目、会话及审计在自有进程退出后精确清理，用户既有状态与服务保持。

> 2026-10-28 项目选择与交接说明修复：原生 Flow 画布上方新增始终可见的项目选择条，显示流程默认项目的名称、完整路径及更换入口；Flow/节点检查器和新建项目对话框共用可视化目录选择器。目录树使用 Qt 控件、中文确认/取消、只读目录模式，不解析符号链接。取消不改草稿，运行中锁定；模态选择返回时重新核对 Flow/节点和运行状态，避免写入已切换的目标。节点显示项目来源、缺失、循环或冲突；同目录合法拼写按 Core 语义预览，已知非法路径给出提示。共享祖先按每次刷新缓存，避免多分支汇合图指数解析。Core 的目录身份、授权和运行前校验均保持。
>
> 保留 Handoff 的自动传递：它把独立节点会话之间的结果和待办交给下游。手工流程/节点交接与文件字段改为中文的可选项目背景，默认收起，已有非空内容自动展开且持久字段保持；节点任务写入“节点指令”。运行详情使用“输入交接/输出交接”。不要求手工背景或专门创建交接文件。详见[Flow 指南](agent-flow.md)。
>
> 缺少选择按钮、等价路径误报、共享祖先重复遍历及非法路径预览均先复现失败再修复；最终新增回归 **41 passed**，联合 Flow 聚焦回归 **63 passed**，独立只读复核无确认发现。真实 WSLg wayland 窗口由应用内 QTest 驱动目录树选择、保存、继承说明和 560 px 检查器，通过中文字体与窗口渲染截图核对，未调用模型，不代表物理鼠标或真实 provider 能力测评。脱敏截图/摘要在 `.cache/reports/flow-project-selection-20261028-j_rddxp4/`；自有临时项目在进程退出后精确清理，用户正在运行的窗口与会话不动。
>
> 本轮开始工作区干净，中途出现不属于本次改动的根 `README.md` 删除，已保留且不混入提交。完整验证从只含本次修复的 Git 索引导出源码副本（保留 HEAD 的 README，无用户 `.env`，复用现有锁定依赖），不恢复或覆盖本机删除。干净源码副本完整非集成回归 **1592 passed、2 skipped、5 deselected（86.08 秒）**；两项跳过为未安装的可选 ANN extra。提交与 PR 以当前 Git/远端状态为准。

> 2026-10-28 远端验收：功能与 CI 修复提交 `a25fc25` 已推送至 `codex/live-project-validation`。GitHub Actions [Regression tests](https://github.com/vukencc/Channel-Agent/actions/runs/40511336224) 成功，包含 locked sync、真实沙箱探针与完整非集成回归；手动集成任务按工作流条件跳过。GitHub API 核对 [PR #29](https://github.com/vukencc/Channel-Agent/pull/29) 为 open、非 draft、`mergeable=true`、`mergeable_state=clean`，main 基线为 `228575f`。尚未执行合并。此文档记录之后若有新提交，以 PR 当前 head 的检查和可合并状态为准。
>
> 干净源码副本的临时验收目录已在进程退出后精确清理，脱敏结果仍在本轮忽略报告目录；本机用户会话、项目、凭据和旧预览均保留。验收时工作区干净，既有项目改动及本轮功能均已提交。Flow 的真实 Core/工具与 Qt 交互由受控模型驱动验证，未额外宣称真实 provider 能力测评。

> 2026-10-27 CI 隔离修复：PR #29 首轮 GitHub 回归发现 19 项失败，原因是旧测试依赖本机 `.env` 的权限/特性设置，以及 `runtime_paths.py` 启动器在 Git 索引中没有执行位。现已让确认、委派和预算测试显式声明各自前提，临时 dotenv 用例只读取自身配置；保留全部原有断言，并恢复共享工具注册表。启动器记录为 100755，未放宽运行时策略或可执行性检查。独立只读复核无确认发现，针对性回归 **226 passed、1 skipped**。
>
> 从修复后的 Git 索引导出干净源码副本（没有用户 `.env`，复用已锁定的现有依赖），确认源码使用默认 smart 策略后，完整非集成回归 **1551 passed、2 skipped、5 deselected（91.63 秒）**。两项跳过仍为未安装的可选 ANN extra。首次副本验收缺少测试约定的 `.venv/bin/python` 路径，补指向现有环境的链接后重跑通过，未重建环境。此修复已推送，远端检查结果见本页最新记录；本地与远端结果分别记录。此前本轮自有 Qt 临时验收目录及进程已精确清理，脱敏截图/摘要仍保存在下述忽略目录，用户旧预览未动。

> 2026-10-27 最终本地验收与提交：全部可分发的既有和本轮项目改动已提交为 `35f5057`，并以 `308a2c1` 正常合入最新 `origin/main`（七个上游提交、自动合并无剩余冲突）。主分支保持保护，沿用 PR #29；尚未直接合并 PR。合并后的最终非集成回归 **1551 passed、2 skipped、5 deselected（101.83 秒）**，两项跳过为可选 ANN extra 未安装；真实 Bubblewrap/prlimit、发布 stdlib 检查、locked sync/lock check、三包构建/资源导入均通过。删除脏 Flow 的最后回归也已包含在最终全量结果中，最终只读复核无确认缺陷。
>
> 最终 Qt 窗口渲染截图和脱敏验证摘要保存在 `.cache/reports/agent-flow-20261027-fgwr2wjj/`（Git 忽略的新报告目录），已逐节点核对真实工具记录。此目录不含真实用户会话或模型凭据。早期及最终验收的独立临时应用/项目目录将在停止其自有进程后精确清理，上文旧临时路径仅作历史说明；不修改现有用户缓存、会话或项目。WSL Git 没有推送凭据，本机 Windows Git 的现有凭据管理器已通过 dry-run 验证，最终推送使用该现有路径，不读取/导出凭据。远端 CI 与可合并状态以 PR 当前检查为准。


> 2026-10-27 原生 Agent Flow 与 GUI 优化：新增“新建项目”Agent/Flow 模式选择、左栏 Flow 列表和原生 QGraphicsView 画布。可拖入/添加 AgentCore、拖动节点、端口拖线形成有向箭头，检查器配置初始提示、项目、文本/文件 Handoff 和模型；运行结果单独显示输入/输出交接并可跳转真实节点会话。图定义与最近运行保存在状态目录 `flows/`，支持保存、停止、重开查看和未保存草稿退出保护。仅原生 GUI 提供 Flow 面板，CLI/Web 经典入口保留；不引入浏览器或新依赖。详见[Flow 指南](agent-flow.md)。
>
> Core 独立 FlowService 校验 DAG、版本、项目身份及模型，按依赖顺序执行；下游继承同一项目和上游最终正文交接，首次模型调用前安全读取 UTF-8 交接文件并明确先读交接。正常 idle、后代/工具/后台命令排空且有最终正文才传递成功；正文标“Agent交接摘要，验证自报”，不能当成独立验证。失败/checkpoint/取消阻断下游。同管理器共用项目预约，只有当前节点会话和真实后代可用，旧运行/前节点不能借持久 flow_id 抢占，交接排空期间根节点不能再次提交；不能确认排空时保留预约。未承诺隔离另一状态目录进程或外部编辑器。主题显式覆盖平台默认 frame，保留弱对比焦点与统一矢量图标，移除对话框平台彩色默认图标。
>
> 已通过受控模型触发真实 Core 工具/后台线程与真实 Qt 控件的集成验证，覆盖确认绑定/拒绝/超时、权限、取消与排空、重开不自动重放、草稿保存失败保留、删除脏 Flow 及模型引用保护。安全与最终审查发现并修复旧节点抢写、无关归档导致清理异常/预约残留、删除草稿的退出问题；复核无确认发现。首轮完整非集成回归 **1549 passed、2 skipped、5 deselected（106.79 秒）**；随后增加的删除回归与最后修复由 canvas/lifecycle **19 passed** 覆盖，合并主分支后的最终回归待下方状态补充。三模块 wheel/sdist 构建及仓库外 Core Flow/GUI/Web/Debug 导入、真实 Bubblewrap/prlimit 探针、当前指南 159 个本地链接与 TOML 检查通过。
>
> WSLg 应用内 QTest 实际拖动、保存、三节点执行和 560 px 检查器已通过，并核对窗口渲染截图及逐节点工具记录：梳理节点无工具，实现节点 create_file，验证节点 read_file，真实读回 proof 文本。模型为明确受控回调，未调用真实 provider，不能称为真实 AI 能力测评或 Windows 物理鼠标验收。早期验收脚本的任务分流过宽已修正，最终证据位于 `/tmp/agent-flow-ui-proof-20261027-n8387vin/`。Windows Computer Use 两次把 WSLg 绑定捕获成其他应用，未据此点击，随后停止 Windows 自动输入；窗口视觉以 Qt 应用自身渲染核对。此前独立 Qt 槽异常和外部保存结果空画布均已补失败回归并修复。
>
> 用户明确要求提交全部项目改动并更新 PR 到可合并：既有三模块/原生 GUI/Debug 修复与当前 Flow 将一并提交，非敏感项目级 `.codex/config.toml`、角色与 AGENTS 通过白名单随仓库提供；认证、`.env`、会话、工作区、日志、缓存和 `docs/dev/` 不入库。沿用分支 `codex/live-project-validation` 和已有 PR #29，不创建重复 PR，不直接合并 main。已提前锁定同步 origin/main 已合入的 FastAPI 0.143.0、Fastembed 0.9.0、OpenAI 3.28.0、PyArrow 26.0.0、Pydantic 2.14.0；无新增运行依赖。后续提交/合并/CI 状态以 Git 和 PR 为准。


> 2026-10-27 Codex 子代理配置优化：按用户要求核对官方 Models、Subagents、model-selection 和 Terra 模型页，将本地角色从七个扩充为十个。新增有界实现 `implementation_worker`、视觉/交互 `ui_specialist` 和只读架构升级 `architecture_advisor`；文档、界面与有界实现使用 Terra，RAG/测试/性能/代码与安全审查使用 6.1 Sol，窄范围只读探查使用 Luna，Astra 仅按需处理未解决的架构决策。模型与推理强度的唯一配置源仍为 `.codex/agents/*.toml`，具体值直接读取文件；未改根模型、认证、权限、三子代理并发上限或应用配置。修正角色指令中旧 `src/ai_agent_startup`、`tests/`、`scripts/`、`dev/` 路径，继续要求明确文件归属、保留共享修改和子代理不再派生。
>
> 十个角色 TOML 的语法、唯一名称、字段、模型/强度组合与指令引用路径检查通过，WSL Codex `0.160.0` 的严格 doctor 返回 `overallStatus=ok`；本轮真实 `docs_writer` 已按宿主注册的 Terra/medium 完成两份协作文档。三个新增角色没有逐一真实派生，严格诊断不代表模型授权或角色全部可用。CLI bundled 离线模型快照没有 Terra，不能据此宣称服务不可用。官方要求 Terra CLI 至少 `0.159.0`；本轮核对 Windows npm CLI 仍为 `0.154.0`，未升级，CLI 验证使用 WSL。宿主未重新发现新增角色时，在同一工作区新开会话后核对；具名角色文件里的模型/强度优先于 spawn 覆盖，不能以临时覆盖参数假定更换角色模型。
>
> `.codex/README.md`、根本地 `AGENTS.md` 与当前[Codex 协作说明](codex-multiagent.md)已同步；`.codex/` 和 AGENTS 仍为被忽略的本地资产，未强制加入版本控制。该方案依据官方角色定位，未进行性能、费用或应用 Agent 能力基准。没有修改应用源码、依赖、`.env` 或用户状态，没有重复运行应用 pytest；上一条 1505 项回归属于目录整合验证。分支仍为 `codex/live-project-validation`、HEAD `08094aa`，没有提交或推送。


> 2026-10-26 进一步合并模块与包目录：用户指出 `AgentCore/agent_core` 等重复层后，确认该层仅为原打包布局惯例，并非功能要求。现已统一为 `src/agent_core/`、`src/agent_test/`、`src/agent_frontend/`；目录同时是 Python 包和独立 uv 项目根，概念模块仍为 AgentCore、AgentTest、Frontend，发行名、Python 导入名和启动命令均保持。测试现在位于 `src/agent_test/tests/`，不进入 wheel，sdist 可包含测试；工程脚本为 `src/agent_test/scripts/`。三个旧大写模块目录已移除，所有包文件在移动前后逐一核对哈希一致，再调整打包配置与路径引用。现有修改和用户数据保留，分支/HEAD 未变，没有提交或推送。
>
> 三模块 wheel 使用显式源码/资源文件集和根映射，editable 直接引用包父目录 `src/`；未增加运行时别名或导入钩子。根发现只接受声明成员中的连续 Python 包路径，继续支持 `AI_AGENT_PROJECT_ROOT` 优先与安装 wheel 的 cwd 回退；成员内 `.venv` 和开发测试不被当成应用配置源。七个目录/根发现断言先失败后通过；针对性检查 **73 passed**，完整非集成回归 **1505 passed、2 skipped、5 deselected（75.04 秒）**，跳过仍为未安装 ANN extra。`uv lock`、locked sync、lock check、三个模块 wheel/sdist 构建、全部源码/资源逐项完整性检查、wheel 不含 tests/pyproject/pyc、同名消费工程成员 `.venv` 中的仓库外导入/根回退及 `.env` 隔离、纯标准库发布入口、真实 Bubblewrap/prlimit 探针均通过。当前指南路径和 CI/Docker 已同步；临时构建目录已清理。本轮未改 UI 或继续操作保留的桌面预览。下文分层目录描述为历史状态，由本条取代。


> 2026-10-26 去除源码嵌套：按用户要求移除三个模块内部的 `src/`，当前包目录为 `src/AgentCore/agent_core/`、`src/AgentTest/agent_test/`、`src/Frontend/agent_frontend/`；模块 `pyproject.toml` 和 `src/AgentTest/tests/` 位置不变。移动前后全部包文件逐一核对哈希一致，再更新 Hatch 包路径、测试资源路径、CI 发布检查和缓存键、工程脚本用法以及当前指南。导入名称和启动命令保持不变；现有未提交改动与用户数据均保留，未提交或推送。
>
> 根目录发现改为识别成员根下的应用包，继续保持显式 `AI_AGENT_PROJECT_ROOT` 优先、安装 wheel 使用 cwd，不会把成员内 `.venv` 识别为应用源码。六个目录/根识别用例在修改前失败、修改后通过；扩展四个成员 `.venv` 回退用例。针对性检查 **73 passed**，最终完整非集成回归 **1505 passed、2 skipped、5 deselected（68.54 秒）**，跳过仍为未安装 ANN extra。`uv lock --check`、locked sync、三个模块 wheel/sdist 构建、仓库外 wheel 导入/资源/根目录识别、无依赖发布入口与真实 Bubblewrap/prlimit 探针均通过；临时构建目录已自动清理。只读审查无确认缺陷。本轮只调整目录与引用，没有更改 UI 或重新操作之前保留的桌面预览。下文目录整合记录为历史状态，内层 `src/` 的描述已被本条取代。


> 2026-10-25 目录整合与 UI 美化：三个 uv 项目统一位于 `src/AgentCore/`、`src/AgentTest/`、`src/Frontend/`，各自保留标准的包级 `src/` 布局；`dev/` 已完整迁至 `docs/dev/`，继续被 Git 与 Docker 忽略。迁移前后 Core/Test/Frontend 文件哈希一致，开发归档 50 个节点的元数据一致；随后只修改必要的代码路径与表现层。工程脚本统一归入可安装的 `agent_test.scripts`，使用 `python -m agent_test.scripts.<名称>`；发布前的 stdlib 检查仍可按新源码路径调用。根 `.env`、会话、工作区、知识库、日志和缓存的归属不变。
>
> Core 新增纯标准库根目录发现 helper，源码须匹配本应用 workspace 身份且处于声明成员的 `src/` 下；独立 wheel 安装使用 cwd，显式 `AI_AGENT_PROJECT_ROOT` 优先。修复了无关/同名 uv 工程安装 wheel 时误读其根配置的审查发现；四项回归先失败后通过。配置同步工具无需导入应用配置，临时无效数值配置不会阻断修复入口。三包已锁定同步并构建 wheel/sdist，仓库外 wheel 验证 Core/Test 一致的 cwd 回退、无误载入 `.env`、RAG JSON 及原生/Web/Debug 资源均通过。
>
> 原生 GUI 新增统一炭灰/浅蓝主题、手绘矢量图标、会话层级与状态、欢迎草稿卡片、消息/代码排版、自适应输入区、滚动设置表单和模型管理；安全格式化仅支持标题/列表/强调/行内与围栏代码，不生成图片或链接。修复模型 JSON 编辑器隐式 HTML 解析与富文本粘贴，输入和 JSON 始终按纯文本处理。WebUI 统一视觉并整理重复 CSS，仅修改普通页面 HTML/CSS；JavaScript 与 Debug 时间线布局保留。
>
> 最终非集成回归 **1495 passed、2 skipped、5 deselected（69.70 秒）**，其中原生相关 24 项通过；跳过仍为未安装 ANN extra。真实 Bubblewrap/prlimit 模块探针、stdlib 发布检查、uv lock/locked sync 与三包构建通过。CI、Docker、当前指南与测试发现路径已更新；没有实际执行 GitHub Actions 或 Docker 构建。只读审查最终无剩余发现。
>
> 原生 WSLg 实际窗口已核对中文、会话导航、消息与代码排版。后续用户以物理 Esc 停止 Computer Use，本轮未继续窗口输入或截图。520×460 窄窗口的会话栏/详情栏与输入、发送可见性由独立 Qt 离屏检查通过，不能表述为后续物理桌面验收。临时受控预览标题为“Agent · 视觉验收（受控样例）”，状态与启动文件位于 `/tmp/agent-ui-polish-20261025-_bnapyfy/`，因用户停止自动操作而保留窗口及目录，未擅自结束；其回调不调用真实 provider。其他本轮离屏报告和构建临时目录已清理。WebUI 由独立临时服务/受控回调检查 Chromium 1440×1000 与 450×840：两会话隔离、长文本/代码、设置、快捷键、流式取消、确认拒绝、侧栏/监控抽屉均通过，滚动 500→500，页面无横向溢出，最终 JS/控制台错误为 0。Web 测试服务已停止、18789 端口释放、临时状态删除；浏览器工具截图仅存在其 Windows 临时目录。以上界面检查未调用真实模型服务。


> 2026-10-25 原生 GUI 与工具简化：保留三模块重组及全部已有未提交改动，分支仍为 `codex/live-project-validation`、HEAD `08094aa`。新增 `Frontend/src/agent_frontend/desktop/` 原生 Qt Widgets 客户端，入口 `ai-agent-gui`，不启动 HTTP、不嵌入 WebView、不依赖浏览器。安装 `uv sync --locked --extra desktop --extra web` 后启动；仅新增 PySide6-Essentials/shiboken6 两个锁定依赖，Core 不依赖 Qt。布局为可隐藏会话栏、中央对话与输入、按需设置/活动/文件预览；支持模型管理、Linux 项目/环境/权限设置、并发会话、确认、取消、历史分页与阅读位置保留、异步安全关闭。正文为转义后的安全纯文本，不渲染 Markdown 格式。使用指南见[原生桌面](desktop.md)。
>
> 用户工具选择已从 Web 表单、CLI `/tools`、HTTP Settings 移除；请求提交 `tool_names` 返回 422。部署能力上限、旧会话限制及子 Agent 收窄继续执行。Core 新建用户会话直接走可信服务，不依赖模型工具注册表；`create_session` 吸收延迟启动能力，常驻补齐单子任务取消；后台 `inspect_command_job` 合并状态与有界日志查询。自动模型目录隐藏四个有可用替代项的兼容名称，旧名称仍可调用，旧名专用策略不被扩大。全部功能启用时兼容注册表 38 项、实际模型目录 34 项；同配置当前目录 JSON 减少 1,390 字符（5.77%），仅为 schema 成本比较，不是 token 账单或模型速度测量。方案与取舍见[工具设计](tool-design.md)。
>
> 最终回归 **1468 passed、2 skipped、5 deselected（62.12 秒）**，含 15 项真实 Qt/Core 桌面测试；跳过仍为 ANN extra 未安装。命令显式 `QT_QPA_PLATFORM=offscreen COMMAND_EXECUTION_ENVIRONMENT=sandbox PYTHONDONTWRITEBYTECODE=1`，禁用 pytest 缓存。真实 Bubblewrap/prlimit 探针、locked sync、三包 wheel/sdist 构建、GUI console entrypoint、102 个现行指南链接与 4 份项目 TOML 通过。在仓库外由 wheel 启动/关闭 GUI，明确阻断 fastapi/uvicorn/AgentTest 导入仍通过。CI offline/release 测试同步增加 desktop extra、offscreen 及所需 EGL 库；没有在本机实际运行 GitHub Actions 或 Docker。
>
> 实际 WSLg 原生窗口通过 Computer Use 检查中文渲染、物理键盘发送、新建/切换与历史恢复、无工具选择的设置、隐藏左栏、流式输出、停止与安全退出。使用独立临时状态及受控模型回调，未调用真实 provider；从窗口触发的真实 create_file 结果与文件内容已独立核对，不能称为真实模型能力测评。Qt 测试另覆盖审批允许/拒绝/过期、两会话并发、凭据清理、状态锁冲突、145 条历史、安全文件预览与资源加载拒绝。独立审查发现并修复设置部分写入、QLabel 隐式 HTML 图片读取和旧 ToolContext 取消兼容分派；复核无剩余发现。临时验收窗口均正常退出，临时状态/项目/构建目录已移除；用户 `.env`、数据与现有服务未改动。


> 2026-10-23 三模块重组：在 `codex/live-project-validation`（HEAD 仍为 `08094aa`）保留全部既有未提交实现后，将源码迁为 `AgentCore/src/agent_core/`、`AgentTest/src/agent_test/`、`Frontend/src/agent_frontend/`。原 `tests/`、`scripts/` 分别迁入 `AgentTest/tests/`、`AgentTest/scripts/`。根目录为 uv workspace；三个模块各有独立 pyproject，可分别构建 wheel/sdist。启动命令不变，Python 导入路径已迁移，见[三模块架构](architecture.md)。没有移动或改写用户状态、项目、知识库、日志与 `.env`。
>
> Core 不再导入 Frontend、AgentTest 或 UI 框架；`SessionManager(observer=...)` 默认 `NullObserver`，即使 `DEBUG=true` 也不隐式记录。Frontend 的 bootstrap 按配置加载 AgentTest 并注入 `DebugRecorder`；Debug API、页面、详情、归档读取和测试工具均归 AgentTest。保留 `manager.debug` 及旧观测函数名作为兼容别名，生产调用使用中性观测接口。安全目录句柄工具迁入 Core，Web 与工作区校验共用，原权限、身份、确认和审计行为保留。Debug UI 布局未重设计。
>
> 本轮重组验证：原有 1413 个收集项完整保留，增加 9 个模块边界用例；非集成回归 **1415 passed、2 skipped、5 deselected（最终集成复跑 58.87 秒）**，跳过仍为未安装 ANN extra。`uv lock`、`uv sync --locked --extra web`、三包 wheel/sdist 构建、资源归属、CLI `--platform`、真实 Bubblewrap/prlimit 探针通过。测试显式 `COMMAND_EXECUTION_ENVIRONMENT=sandbox`、`PYTHONDONTWRITEBYTECODE=1` 并禁用 pytest 缓存。独立进程覆盖 Core 在 Test/Frontend/UI 库不可导入时执行 headless，及 Frontend 在 AgentTest 不可导入时普通页面/API 可用。
>
> 本轮实际浏览器使用独立临时服务与受控元数据 fixture，未调用真实模型：普通页面、当前树 3 条 Agent 轨道、另一根隔离、异常/拒绝、42 px 标签与短竖箭头、局部概览、显式脱敏详情、滚动 560 px 后选择仍为 560 px、450 px 右侧抽屉均通过，Debug 控制台错误为 0。普通页面只有既有 favicon 404。从构建 wheel 解包后，在仓库外启动普通/Debug HTTP 与静态资源检查通过；107 个现行指南链接和 4 份项目 TOML 通过，259 个受跟踪的旧源码/测试/脚本均有迁移目标。独立只读审查无确认发现。CI、Docker、现行指南中的源码与脚本路径已同步；WSL 未启用 Docker Desktop 集成，镜像构建未实测，不能报告为通过。下文旧结果与已清理产物仍是历史事实。


> 上一轮状态（2026-10-16）：当前分支 `codex/live-project-validation`、HEAD 为 `08094aa`。本轮保留全部已有未提交改动，继续修复 DEBUG，并增加用户选择的 Linux 本机执行环境；尚未提交或推送。既有功能提交为 `c0cd0dd`，先前交接记录关联 PR #29；远端与 PR 当前状态未在本轮重新核验。

> 清理状态：用户随后明确选择只清理报告、截图、缓存和临时测试项目，保留功能代码、回归测试、辅助脚本与配置。已删除仓库 `.cache/reports/`、pytest/源码字节码缓存及已停止的 Linux 临时测试项目，共约 1 GB；331 个源码、文档和测试文件及 `.env` 在清理前后核对一致。本次仅补充此交接状态。下文验证数字作为历史事实保留，所列生成报告、截图和临时项目路径已不可用，不要尝试从原路径继续实验。测试浏览器存储已清除、页面已关闭；Windows `.playwright-mcp` 生成文件的批量及具体文件删除均被执行安全策略拒绝（未给出具体原因），该部分仍保留。用户会话、运行工作区、原始数据、应用日志和依赖环境未清理。

新会话开始时先运行 `git status --short`，保留所有已有状态。不要执行 reset、clean、rebase，也不要切换到旧 tag，以免丢失当前长期实现。`.agent/`、`crud_tests/`、`data/raw/`、`logs/` 和 `.cache/` 含用户状态或生成数据，应保留；需要新报告时只在 `.cache/reports/` 下新建。不要打印 `.env` 内容或密钥，也不要自动重跑真实模型任务或清理缓存。

## 建议阅读顺序

1. 根目录 `AGENTS.md`：本地工作和代理调度约定。
2. 本文：当前工作状态、环境和最近验证事实。
3. [文档索引](README.md)：现行指南、设计边界与历史记录分类。
4. 按工作内容阅读对应指南。DEBUG 以[运行调试说明](debug-runtime.md)和[清晰度报告](debug-overview-clarity-report.md)为准；旧交付截图不能代表当前布局。

## 当前实现与验证边界

DEBUG 总览根与子 Agent 均为 224 px，子 Agent 不再自动折叠。按根或子 Agent 筛选时包含其后代行为；祖先仅保留必要结构，不混入祖先行为。共享 fork/join 母线表达父子结构，不交叉；通信在局部详情中逐条呈现。异常按 session、turn、tool_call_id 去重，保留原始记录与上下文/token 信息。

用户明确要求保持原 DEBUG 主图布局，禁止擅自重设计。长横折线、编号层和混合方向摘要已撤回；保留两行 42 px 标签和短竖箭头，只将箭头定位到真实代表事件时间，不伪造间距。按用户最新要求增加隐藏左栏开关和右侧独立滚动详情，窄屏为右侧抽屉；点击详情不再 `scrollIntoView`。局部“发生了什么”从区间全部元数据概览阶段、去重工具结果、创建/等待与通信，单事件行可单独解释；不额外调用模型，不自动读取正文，不把等待调用完成等同子任务完成。跨轮长空闲修复保留，空闲压缩仍可由用户开关，实际清理与未完成子任务仍计时，停止后不随刷新延长横轴。

WebUI 调试入口现在带当前 `session`，API 的 `session_id` 归一到所属根，只返回该树的 Agent、时间线、legacy span、活动、指标、丢弃计数和详情。直接打开调试页默认选一个活动或最近根后固定范围，不展示全进程历史。generation 绑定根和排序后的成员集合，切根及子 Agent 新增/删除时重建缓存，避免旧事件再次生成残留轨道；已删除根返回错误并停止刷新，不跳到其它会话。范围隔离在服务端完成，前端轮询、窗口重建和详情读取均携带范围。

Linux 根会话可在新建/设置中输入已有项目目录，设置中可浏览目录；CLI 为 `/project /绝对路径`，`/project default` 恢复默认。外部根持久化路径及 dev/ino，子 Agent 继承，运行或排队任务/工具/后台命令未结束时不能切换；切换与删除会话均保留项目。模型工具不提供宿主根选择参数。拒绝系统/家目录根、应用源码/凭据/状态/只读数据重叠与符号链接；Web 预览持有目录句柄验证身份，CRUD 审批后再次核对根，清理保护包含失效与回收会话的外部路径。沿用本机单用户工具安全模型，未将全部 CRUD 重构为全程目录句柄操作。

新增可信用户选择 `sandbox/local`：WebUI 新建/设置和 CLI `/environment` 可选，子 Agent 与分支继承有效环境，模型会话工具不能自行选择或提权。源配置 `COMMAND_EXECUTION_ENVIRONMENT=sandbox`，本机命令默认超时 `LOCAL_COMMAND_TIMEOUT=600` 秒；本轮仅在现有 `.env` 中原子设置 `COMMAND_EXECUTION_ENVIRONMENT=local`，其余配置与凭据保留，用户服务重启后生效。没有依赖变更。

Linux local 命令使用宿主真实路径、工具、网络和 localhost，不施加 Bubblewrap、白名单、RLIMIT 或工作区配额；仍遵循当前权限策略、确认、审计、根身份校验、输出界限、超时与取消。子进程仅继承运行时与标准代理变量，不继承模型密钥，环回地址补入 `NO_PROXY`。真实测试发现过滤系统代理会阻止下载，已先复现再修复。本机模式自动提供环境准备与后台命令能力；服务存活期间不独占整个工作区锁，允许前台探测和编辑。文件整理 `mkdir/move/copy` 也跳过 local 下的沙箱配额，但保留原子、不覆盖、权限和身份边界。只读审查发现的 `/proc` 追踪初始化/清理异常泄漏进程问题已复现修复，Popen 后始终兜底终止进程组、wait 和关闭管道；进程树仍属尽力回收。JDK/Maven/数据库仍需真实安装或启动；本机模式不保证宿主已安装它们，也不提供隔离安全保证。隔离失败绝不自动回退本机。

隔离模式保留 `run_command(resource_profile="development")` 显式开发额度、联网白名单及 runtime 内存档位。uv 环境准备允许显式 `allow_scripts=true` 源码构建/项目安装，默认仍 wheel-only 且不安装项目自身。CI 沙箱探针已固定显式 sandbox，避免服务默认 local 时错误报告隔离通过。

本轮真实模型完成“团队资产借用管理系统”：Java 17 + Spring Boot + Maven 后端、React/TypeScript/Vite 前端、H2 文件数据库、BCrypt/JWT、角色权限、资产 CRUD/搜索分页、库存借还事务、仪表盘与审计。**1 主 Agent + 3 子 Agent，170 次真实 provider 请求、251 次工具调用，总墙钟 1891.115 秒**，包括首次网络故障 411.071 秒及恢复停顿。最初因应用过滤系统代理有序停止，修复后从同一根会话及原代码继续，未人工代写或修改模型业务代码。两个实际进程 trace 共 6181 事件，均无丢弃；当前恢复后的 Debug 为 5817 事件。工具链由真实 Agent 下载并展开，实际验证 Java 17.0.2/Maven 3.9.16，未预先安装业务依赖。

父任务独立重跑 Maven test/package、前端 test/build，四条命令退出 0；**JUnit 19 项、Vitest 6 项通过，独立验收 14/14 通过**。验收覆盖真实子 Agent 调用、JWT、400/401/403/404、库存不足不落借用、越权归还、审计仪表盘、H2 服务重启持久化及构建后预览的真实 API 代理。实际浏览器验证管理员新增/编辑/搜索/空状态，成员借用 2 件使库存 3→1、不足库存拒绝无新增记录、归还后 1→3、刷新持久化、成员管理页重定向、管理员审计和 450 px 无横向溢出。页面无 JavaScript 运行异常；演示项目有一个不影响功能的 favicon 404。部分自动化原生点击未派发时通过 DOM click 触发实际 React 事件；没有替换 HTTP 结果，不能将此描述为人工鼠标可用性验证或应用 Agent 自己完成的浏览器 E2E。数据库为真实 H2，不代表原 MySQL 项目已验证。

真实 Debug 浏览器确认 4 条轨道、3 个子 Agent 完整行为、异常与脱敏详情、根筛选包含后代，保留原 42 px 标签与短竖箭头；滚动到 560 px 后点局部仍为 560 px，右栏 sticky top 12 px。450 px 窄屏抽屉在文档范围内；选择概览不读取正文，显式读取脱敏详情正常。四 Agent 全部 idle 后继续刷新，时间轴前后均为 22.8 min，没有随挂起页面延长。清理前报告入口为 `.cache/reports/fullstack-capability-20261016-f37a/`，原始模型状态、项目及私有日志曾保存于 `/tmp/agent-real-multi-validation-3a6eb9c810dc45e39d947bd3e27b73c9`；两处均已按用户后续要求删除，验证结论仅保留本文摘要。验收后已精确关闭本轮独立服务，18870/18911/18912 均确认释放，未重启或停止用户原有服务。

测试中监督器 7 次请求有 3 次 ValueError、1 次共享截止时间取消，已降级后主任务完成；未保留 HTTP 状态，不能猜测具体网络或校验原因。归档共享截止时间造成后续块本地 fallback，不等同逐块新网络请求；成本单价未知，不能把估算 0 描述为免费。以上边界在安全摘要中保留。

代码修复后的非集成回归为 **1406 passed、2 skipped、5 deselected（52.66 秒）**；两项跳过均为未安装可选 ANN extra。因本机 `.env` 已选择 local，回归命令显式设置 `COMMAND_EXECUTION_ENVIRONMENT=sandbox` 固定隔离基线，local 行为由明确 local context 用例覆盖。真实 Bubblewrap/prlimit 探针、9 份当前指南的 47 个本地链接通过。只读审查确认的分支继承、进程清理、探针误报和 `failed` 叙述遗漏均已修复复核，后续无确认发现。不要与下述历史结果混淆。

既有真实模型实验 2 完成：1 个父 Agent 与 2 个子 Agent，740.174 秒、125 次模型请求、158 次工具调用、6 条发送和 9 条读取消息、0 次 Compact，峰值上下文利用率 64.49%。既有 4460 个事件、3 个 Agent 的 trace 仍保留，它不是新模型调用。本轮浏览器使用另外生成的 49 事件、3 Agent 受控 UI 元数据 fixture，不代表模型或工具执行。

此前 DEBUG 纠正后的非集成回归为 **1298 passed、2 skipped、5 deselected**，用时 38.31 秒；只读审查发现并修复成员删除后的缓存残留，复核后无发现。当时浏览器使用独立临时状态、两个根会话和三个当前树 Agent 的受控元数据，核验两棵树分别打开互不混入、原 42 px 两行标签、无编号层、箭头仅竖线、子 Agent 展开、异常/通信/局部详情、一小时空闲折叠和 450 px 窄屏（文档宽 435 px），页面 JavaScript 错误为 0。该历史报告在 `.cache/reports/debug-session-simple-20261016-e9a4/`，验证服务已结束；该轮未调用真实模型、未重跑用户任务，也未启动或停止用户服务。本轮新测试独立记录在上文。

上一轮环境/工作区验证为 1281 passed、2 skipped、5 deselected，用时 38.28 秒；真实 Bubblewrap + prlimit 探针通过。该轮实际外部目录选择/预览、系统目录拒绝和临时 uv 源码构建等工程结果保留，报告在 `.cache/reports/workspaces-debug-20261016-cb63f2/`。该目录中的编号/折线 UI 截图已被本轮纠正，不代表当前布局；49 事件受控 fixture 构造脚本仍在 `.cache/reports/debug-idle-order-20261016-a41f/`。以上结果不是后续改动的永久保证。

此前文档交接检查覆盖 45 份 Markdown、206 个本地链接和 8 份 TOML；链接、锚点、索引和配置解析通过。当时 WSL Codex 0.160.0 的 `codex --strict-config doctor --json` 返回 `config.load: ok`，指令预览可读取新版 AGENTS 与交接路径；未以真实推理验证每个自定义角色。详细验证清单在 `.cache/reports/docs-handoff-20261016-02782df1/validation.json`。本轮浏览器验证只启动独立临时状态和项目的验证服务，结束后关闭，不触碰用户正在运行的服务。

## 运行环境

项目实际运行环境为 Linux/WSL：仓库 `/home/elaine_vuken/projects/ai-agent-startup`，`uv` 位于 `/home/elaine_vuken/.local/bin/uv`，Node 来自 `/home/elaine_vuken/.nvm/versions/node/v24.21.0/bin/node`，Python 使用项目 `.venv/bin/python3.12`。系统 Python 3.14 与项目虚拟环境不同。非登录 shell 中使用 Node 前需把上述 nvm `bin` 目录加入 `PATH`。

Windows 桌面当前工作目录可能显示为 `\\wsl$\Ubuntu\home\elaine_vuken\projects\ai-agent-startup`；从该 UNC 路径运行 Windows `uv` 可能重建 `.venv`。应在 WSL Linux 中运行 `uv`、Python 和 Node。通常安装命令为 `uv sync --locked --extra web`；依赖没有变化时，可用 `uv run --no-sync ...` 避免同步环境时移除现有可选依赖。此前记录中的 `UV_NO_SYNC=1 uv run ...` 是当时使用的命令形式。

回归应在 Linux/WSL Node 环境运行，避免 FakeDOM 缺失导致相关测试误跳过。`AgentTest/scripts/ci_sandbox_probe.py` 会实际检查 Linux 沙箱能力；不支持时如实报告。最近报告和截图在 `.cache/reports/debug-clarity-20261016-b84e14/`，此前 `debug-flow` 截图已被新布局替换，不要删除旧产物。

## 版本控制边界

本地 `AGENTS.md`、`.codex/` 和 `dev/` 被 Git 忽略，当前 checkout 内仍可供新会话读取，但 fresh clone 不会包含它们。推送与 PR 状态以远端分支和 GitHub 页面为准；不要把本地提交描述为已同步，除非远端已确认。新会话可先用一句话提示：“先阅读根 AGENTS.md 与 docs/session-handoff.md，检查 git status 并保留全部现有改动，再按 docs/README.md 找对应指南。”
