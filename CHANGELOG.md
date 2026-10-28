# 更新日志

格式参考 Keep a Changelog；版本号遵循语义化版本；每个 tag 对应一个可运行基线。

## [Unreleased]

### Added
- 原生桌面和 WebUI 提供 Agent Flow 画布，支持节点、项目与交接设置、有向连接、保存和运行状态查看；每个节点由 Core 会话执行。
- 可选原生 Qt 桌面入口 `ai-agent-gui`，直接使用 AgentCore，提供会话、流式对话、模型管理、项目设置、安全文件预览与确认，无需浏览器。
- 可配置的 Agent 任务计划工具，支持依赖步骤、执行关联、证据记录与中断恢复；CLI 提供只读查看，WebUI 提供只读计划看板。该功能默认关闭。
- 可选本机 WebUI，支持会话管理、任务监控、文件预览及清理缓存、日志和会话记录。
- CLI 命令搜索与补全、会话权限模式选择、会话回收维护，以及 Smart 风险规则和显式权限申请。

### Changed
- 源码模块统一收纳至 `src/`，本地开发归档迁至 `docs/dev/`；工程脚本归入可安装的 `agent_test.scripts`，同步构建、CI 与文档入口。
- 统一原生桌面与 WebUI 的视觉层次、导航、输入区、对话及设置窗口，保留已有会话操作与权限流程。
- 工具由框架自动管理，移除 Web/CLI 用户工具选择与 HTTP 设置字段；模型目录统一重复委派、状态和后台日志入口，用户新建会话改为 Core 服务直接执行，并补齐默认子任务取消。
- **Python 导入路径迁移**：项目拆为 `AgentCore`、`AgentTest`、`Frontend` 三个 uv workspace 包；内核通过中性观察器接口连接 Debug，CLI/WebUI 和诊断资源各自归属独立模块，原有启动命令与用户数据位置保留。
- CI 对常用开发分支 push 执行回归并安装 WebUI 可选依赖；发布 tag 校验包版本、锁文件、更新日志、注解格式及 `main` 提交，并新增 PR 模板。
- 项目级 Codex 默认配置与根 `AGENTS.md` 随仓库提供；本地开发资料继续排除，工程脚本随 AgentTest 分发。
- 整理文档索引、历史报告标注与新会话交接入口；修正文档中的现行沙箱探测脚本路径。
- 更新结构化历史监督评分、WebUI 上下文状态及隔离项目验收说明。
- 监督评分覆盖软保护逻辑组，硬预算允许归档最新完整组；监督响应拒绝截断或含旁白的内容，并支持独立 thinking 模式配置。

### Fixed
- Flow 画布和检查器可视化选择项目目录，显示默认项目及节点继承来源；自动交接明确说明，手工背景改为可选并保留既有内容。
- 共享工作区的文件与图片读取和命令操作共用锁，避免父目录符号链接竞态。
- 修复取消后 Web 计划读取、归档计划分页、CLI 清理与删除并发互等，以及发送期间新草稿被覆盖的问题。

## [0.2.1] - 2026-09-28

### Changed
- Docker 镜像和开发环境固定使用 PyTorch CPU 索引，默认 reranker 使用 CPU 推理并缩小镜像体积。

## [0.2.0] - 2026-09-28

### Changed
- **破坏性**：应用代码迁移到 `src/ai_agent_startup/` 包布局，测试迁移到 `tests/`，Docker 资产迁移到 `docker/`。
- **破坏性**：启动命令由 `uv run main.py` 改为 `uv run ai-agent-startup`（等价：`uv run python -m ai_agent_startup.main`）；项目经 hatchling 安装为包，`uv.lock` 同步更新。
- 全部内部导入改为 `ai_agent_startup.*`；`dev/` 保留诊断、性能基准、RAG 数据准备与历史报告。
- 新增可选环境变量 `AI_AGENT_PROJECT_ROOT`，用于覆盖自动探测的仓库根目录。

### Fixed
- Docker 构建命令更新为 `docker build -f docker/Dockerfile ...`，compose 使用 `docker compose -f docker/docker-compose.yml ...`。

## [0.1.1] - 2026-09-28

### Added
- MIT 许可证、`.python-version` 与 `.gitattributes`。
- Docker 支持：`Dockerfile`、`.dockerignore`、`docker-compose.yml`、Docker 指南。
- 标签触发的发布工作流：离线回归 + 沙箱探测 + 构建并推送 GHCR 镜像 + 创建 GitHub Release。
- 模板/分支/发布约定：`CONTRIBUTING.md`、`docs/release.md`。

## [0.1.0] - 2026-09-28

### Added
- 多会话全屏 CLI：历史、记忆、独立工作区、并发会话、导出与恢复。
- 工具注册表与 Bubblewrap 命令沙箱：文件 CRUD、分段追加、命令执行、联网搜索、混合 RAG 检索。
- 混合 RAG：向量 + BM25 + RRF + 本地 CrossEncoder 重排，父块返回与引用编号。
- 20 项缺陷修复（`dev/bug_report/`）与 28 项性能/自由度优化（`dev/optimization_report/`）。

### Known limitations
- 全量模型推理峰值内存、RAG p95/吞吐权衡、真实视觉服务、Windows/macOS 原生能力尚未验收，详见 `docs/optimization-delivery.md`。
- 容器后端（Docker/Podman 作为命令沙箱）仅有设计契约，未实现，见 `docs/platform-design.md`。

[Unreleased]: https://github.com/vukencc/Channel-Agent/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/vukencc/Channel-Agent/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/vukencc/Channel-Agent/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/vukencc/Channel-Agent/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vukencc/Channel-Agent/releases/tag/v0.1.0
