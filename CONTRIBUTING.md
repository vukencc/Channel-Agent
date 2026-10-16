# 贡献指南

## 准备环境

项目要求 Python 3.12+。从仓库根目录安装锁定依赖（含 WebUI 可选依赖），只在 `.env` 不存在时创建本地配置：

```bash
uv sync --locked --extra web
if [ ! -f .env ]; then cp .env.example .env; fi
```

填写模型服务凭据后，可启动全屏 CLI 或本机 WebUI：

```bash
uv run --no-sync ai-agent-startup
uv run --no-sync ai-agent-web
```

CLI 支持 headless 单次任务：`uv run --no-sync ai-agent-startup --prompt "任务" --json --policy readonly`；`--platform` 只报告平台能力。Headless 默认 `readonly`，待确认操作会拒绝。可用选项以 `uv run --no-sync ai-agent-startup --help` 为准。

## 开发流程

开始前检查当前分支和工作区，保留已有未提交改动与用户数据。不要为了开始工作切回旧 tag；在当前基线状态上创建主题分支，或使用独立 clone/worktree。合并到 `main` 通过 PR，不直接 push 或 force push。分支和发布约定见[发布说明](docs/release.md)。

建议使用 `feat/`、`fix/`、`perf/`、`docs/`、`ci/`、`chore/` 前缀。提交信息用简短前缀说明行为，例如：

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
uv run --no-sync pytest -m 'not integration' -q
uv run --no-sync pytest tests/test_command.py -q
uv run --no-sync python scripts/ci_sandbox_probe.py
```

沙箱探测需要 Linux 上的 Bubblewrap/prlimit 环境。真实模型集成测试需要本地模型与公开语料：

```bash
RUN_RAG_INTEGRATION=1 RAG_QUALITY_CORPUS=/path/to/corpus.parquet uv run --no-sync pytest -m integration -q
```

CI 和常用脚本位于 `scripts/`，不是 `dev/`。测试、安全边界和配置细节见[文档索引](docs/README.md)。

## Pull Request

- 说明用户可见行为变化，并列出实际运行的验证命令与结果。
- 新配置同步写入 `.env.example` 和相关文档；依赖变化同步更新 `uv.lock`。
- 保持沙箱 fail-closed、权限确认、路径边界和审计规则。
- 不提交 `.env`、`.agent/`、`crud_tests/`、`logs/` 或 `.cache/` 中的本地状态和生成数据。

## 发布与容器

版本冻结和标签流程见[发布说明](docs/release.md)；Docker 构建与运行见[Docker 指南](docs/docker.md)。
