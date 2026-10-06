# 贡献指南

## 快速开始

```bash
uv sync --locked
cp .env.example .env   # 仅在 .env 不存在时；填写模型凭据
uv run ai-agent-startup         # 交互式全屏 CLI
```

无图形终端时使用 headless 入口（默认拒绝一切待确认操作）：

```bash
uv run ai-agent-startup --prompt "把 hello 保存到 hello.txt 并读回" --json
uv run ai-agent-startup --platform        # 平台/沙箱能力说明
```

## 分支模型

- `main` 是模板/稳定分支，只通过 PR 合入，不直接 push、不 force push。
- 历史版本用注解 tag 冻结（当前基线 `v0.2.1`），tag 一旦推送不再移动。
- 新方向从基线 tag 开分支，而不是从 `main` 的最新提交随意分叉：

```bash
git switch -c feat/<topic> v0.2.1
# 并行开发（避免反复切分支）
git worktree add ../ai-agent-<topic> v0.2.1 -b feat/<topic>
```

- 分支前缀：`feat/`、`fix/`、`perf/`、`docs/`、`ci/`、`chore/`、`exp/`（实验性方向）、`codex/`（Codex 工作分支）。
- 长期分歧较大的方向建议用 GitHub「Use this template」新建独立仓库，避免同仓长期分叉。

## 提交信息

使用前缀加简短说明，正文写清行为变化与验证命令：

```text
feat: 支持命名知识库的时间过滤
fix: 修正 headless 模式下确认默认值
perf: 有界缓存重排结果
docs: 补充 Docker 使用说明
ci: 标签触发镜像发布
chore: 更新依赖锁定
```

## 测试

```bash
uv run pytest -m 'not integration' -q          # 离线回归，必须全绿
uv run pytest tests/test_command.py -q           # 针对性回归
uv run python scripts/ci_sandbox_probe.py      # Linux：真实 Bubblewrap/prlimit 探测
```

可选（需要本地模型或公开语料）：

```bash
RUN_RAG_INTEGRATION=1 RAG_QUALITY_CORPUS=<parquet> uv run pytest -m integration -q
```

## PR 清单

- [ ] 离线测试全绿；针对性测试覆盖新行为与失败路径
- [ ] 未放宽安全语义：沙箱 fail-closed、确认、审计、路径边界、工具调用配对
- [ ] 新配置写入 `.env.example`，行为变化同步 `docs/`
- [ ] 依赖变更同步 `uv.lock`（`uv lock` / `uv sync --locked`）
- [ ] 未提交用户数据：`.agent/`、`crud_tests/`、`logs/`、`.cache/`、`.env`
- [ ] 性能改动附优化前后测量方法与数字

## 发布

版本冻结、tag 与 Release 流程见 [发布说明](docs/release.md)。
Docker 构建与运行见 [Docker 指南](docs/docker.md)。
