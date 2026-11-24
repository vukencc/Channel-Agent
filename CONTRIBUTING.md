# 贡献指南

首次使用先读[快速上手](docs/getting-started.md)，了解项目后再读[三模块架构](docs/architecture.md)。本页说明开发、验证与 PR 的最短流程；阶段成果和已知边界见[阶段状态](docs/project-status.md)。

## 准备环境

项目要求 Python 3.12+。从仓库根目录安装锁定依赖（含 WebUI 可选依赖），只在 `.env` 不存在时创建本地配置：

```bash
uv sync --locked --extra web --extra desktop
if [ ! -f .env ]; then cp .env.example .env; fi
```

在 Linux/WSL 中运行；WSL checkout 不使用 Windows uv/Python。原生 GUI 需图形会话及 Qt 系统库，WebUI 无需 Node 构建；依赖与排障见[桌面指南](docs/desktop.md)和[快速上手](docs/getting-started.md)。填写模型服务凭据后，选择一个入口：

```bash
uv run --no-sync ai-agent-startup
uv run --no-sync ai-agent-web
uv run --no-sync ai-agent-gui
```

CLI 支持 headless 单次任务：`uv run --no-sync ai-agent-startup --prompt "任务" --json --policy readonly`；`--platform` 只报告平台能力。Headless 默认 `readonly`，待确认操作会拒绝。可用选项以 `uv run --no-sync ai-agent-startup --help` 为准。

## 开发流程

开始前检查当前分支和工作区，保留已有未提交改动与用户数据。不要为了开始工作切回旧 tag；在当前基线状态上创建主题分支，或使用独立 clone/worktree。合并到 `main` 通过 PR，不直接 push 或 force push。分支和发布约定见[发布说明](docs/release.md)。

人工贡献可使用 `feat/`、`fix/`、`perf/`、`docs/`、`ci/`、`chore/` 前缀；Codex 按根 [AGENTS.md](AGENTS.md) 使用 `codex/<topic>`。提交信息用简短前缀说明行为，例如：

```text
feat: 支持命名知识库过滤
fix: 修复 headless 确认行为
perf: 限制重排缓存
docs: 更新 WebUI 指南
ci: 校验发布版本
chore: 更新依赖锁定
```

## 验证

```bash
QT_QPA_PLATFORM=offscreen COMMAND_EXECUTION_ENVIRONMENT=sandbox uv run --no-sync pytest -m 'not integration' -q
COMMAND_EXECUTION_ENVIRONMENT=sandbox uv run --no-sync pytest src/agent_test/tests/test_command.py -q
uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe
```

沙箱探测需要 Linux 上的 Bubblewrap/prlimit 环境。真实模型集成测试需要本地模型与公开语料：

```bash
RUN_RAG_INTEGRATION=1 RAG_QUALITY_CORPUS=/path/to/corpus.parquet COMMAND_EXECUTION_ENVIRONMENT=sandbox uv run --no-sync pytest -m integration -q
```

Node 用于 Web/Debug 的 JavaScript 回归；WSL 中应检查 Linux/nvm PATH。真实 RAG 模型测试与付费模型业务验证分别准备环境，不把离线 fixture 当成真实调用。CI 和常用脚本位于 `src/agent_test/scripts/`；`docs/dev/` 不随 clone 分发。测试、安全边界和配置细节见[文档索引](docs/README.md)。

新缺陷先用 `src/agent_test/tests/test_<feature>.py` 复现，再修复；工具测试复用 `sandbox_env`，文件使用 `tmp_path`。文档变更检查本地链接、示例命令与实际入口，历史报告保留当时结果，勿以旧数字代替本次验证。三个模块的构建与资源检查见[架构说明](docs/architecture.md)。

## Pull Request

- 说明用户可见行为变化，并列出实际运行的验证命令与结果。
- 新配置同步写入 `.env.example` 和相关文档；依赖变化同步更新 `uv.lock`。
- 保持沙箱 fail-closed、权限确认、路径边界和审计规则。
- 不提交 `.env`、`.agent/`、`crud_tests/`、`logs/` 或 `.cache/` 中的本地状态和生成数据。

## 发布与容器

版本冻结和标签流程见[发布说明](docs/release.md)；Docker 构建与运行见[Docker 指南](docs/docker.md)。

## Codex 云端审查与合并

在 [Codex 审查设置](https://chatgpt.com/codex/cloud/settings/code-review) 选择本仓库，启用自动代码审查，并按需要选择所有 PR 和每次推送。新提交尚无审查时，有写权限的维护者可在 PR 评论中发送 `@codex review`。审查使用连接到 GitHub 的 ChatGPT 账户。

GitHub Actions 收到官方 Codex 审查摘要或回归工作流完成事件后，会核对同一 PR、提交及主线版本上的 Linux `offline` 和 Windows `windows` 作业。只有最新提交完成审查、零发现、讨论已解决、分支与主线同步，才会调用 GitHub 合并接口。每小时复查仍开放的 PR。没有可验证的机器人摘要、无法关联 CI 的 fork、草稿、带 `hold` 标签的实验会继续保持开放。修改工作流、仓库内 GitHub Action 或门禁脚本的 PR 由维护者单独审查合并。[工作流](.github/workflows/codex-automerge.yml) · [官方审查说明](https://learn.chatgpt.com/docs/third-party/github)
