# 更新日志

格式参考 Keep a Changelog；版本号遵循语义化版本；每个 tag 对应一个可运行基线。

## [Unreleased]

### Added
- 可配置的 Agent 任务计划工具，支持依赖步骤、执行关联、证据记录与中断恢复；CLI 提供只读查看，WebUI 提供只读计划看板。该功能默认关闭。
- 可选本机 WebUI，支持会话管理、任务监控、文件预览及清理缓存、日志和会话记录。
- CLI 命令搜索与补全、会话权限模式选择、会话回收维护，以及 Smart 风险规则和显式权限申请。

### Changed
- CI 对常用开发分支 push 执行回归并安装 WebUI 可选依赖；发布 tag 校验包版本、锁文件、更新日志、注解格式及 `main` 提交，并新增 PR 模板。
- 取消跟踪本地开发资料与 Codex 配置，保留 CI 探测和常用 RAG 工具于 `scripts/` 供 clone 与 CI 使用。

### Fixed
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
