# 发布与版本冻结

> 文档类型：当前使用指南 · [文档导航](README.md) · [快速上手](getting-started.md)

## 版本与标签

- tag 命名 `v<major>.<minor>.<patch>`，使用注解 tag（`git tag -a`）；**tag 一旦推送不再移动**。
- `main` 是模板/稳定分支，受保护，只通过 PR 合入。
- 每个 tag 对应一个可运行基线，变更记录写入 `CHANGELOG.md`。
- 发布版本先更新根及三个模块的 `pyproject.toml`、模块间精确依赖、`uv.lock` 与 `CHANGELOG.md`，经 PR 合入 `main`；tag 版本须与全部包版本及变更记录标题一致。开发分支的未发布功能见 [阶段状态](project-status.md)，不能假定已有镜像包含它们。
- 标签工作流要求 tag 为 `vN.N.N` 格式、注解 tag，且目标提交已位于 `origin/main`。已推送的 tag 不移动。
- 发布镜像由标签工作流构建，镜像标注 `org.opencontainers.image.revision` 关联提交。

## 发布清单

Windows 独立 exe、可选 ZIP 与 Linux x64 tar.gz 的本地构建、自检和校验值见 [Windows 指南](windows.md)及 [Linux 成品指南](linux.md)。构建包不包含 `.env`、会话、项目、日志或模型权重。Windows 默认构建为单 exe，目录版需保留 `_internal`；对外分发须保留依赖许可证及随包资料。本地未提交源码构建须按清单中的 `working_tree_modified` 标记，不得冒充已发布 tag。

1. 同步根和 `src/agent_core/`、`src/agent_test/`、`src/agent_frontend/` 中的包版本及精确模块依赖，运行 `uv lock`；在 `CHANGELOG.md` 添加对应版本标题。通过 PR 合入 `main`，不要直接 push `main`。
2. 确认没有待保留的未提交改动，再拉取和快进本地 `main`；有开发工作时先完成交付或使用独立工作区，不能重置或清理：

   ```bash
   git fetch origin
   git switch main
   git merge --ff-only origin/main
   git status --short
   ```

3. 确认版本号严格匹配 `vN.N.N`，且与 `pyproject.toml`、`uv.lock` 的包版本和 `CHANGELOG.md` 对应标题一致；工作流还会检查注解 tag 及提交是否位于 `origin/main`。
4. `uv sync --locked --extra web --extra desktop` 成功，与发布 CI 的依赖选择一致；Linux 安装 Bubblewrap、util-linux 和 Qt 系统库。
5. `QT_QPA_PLATFORM=offscreen COMMAND_EXECUTION_ENVIRONMENT=sandbox uv run --no-sync pytest -m 'not integration' -q` 全绿。
6. `uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe` 通过（Linux），分别构建三个 wheel 并核对静态资源，见[架构](architecture.md)。
7. 可选：按 [RAG 指南](rag.md) 准备模型/语料，再运行 `RUN_RAG_INTEGRATION=1 RAG_QUALITY_CORPUS=/path/to/corpus.parquet uv run --no-sync pytest -m integration -q`。记录未测或跳过项，不把包版本校验当成业务验收。
8. 创建并推送注解 tag；不要直接推送 `main`：

   ```bash
   git tag -a vX.Y.Z -m "发布说明摘要"
   git push origin vX.Y.Z
   ```

9. 标签推送触发 `release.yml`：先执行 Linux 与 Windows 回归，再构建双平台桌面包和 GHCR 镜像；全部成功后创建 GitHub Release，上传独立 exe、ZIP、tar.gz 与各自的 SHA256。必须检查所有任务及发布资产，不能只检查 Release 页面是否存在。手动 `workflow_dispatch` 仅执行两端回归和成品构建，不推镜像或创建 Release，可在标签发布前验收。
10. 冷备份：`git bundle create ai-agent-startup-vX.Y.Z.bundle --all`，存放到仓库之外的介质。

## 回滚

- 代码：在干净的独立工作区从明确选择的目标 tag 开修复分支（如 `git switch -c codex/release-repair vX.Y.Z`），修复后经 PR 按补丁版本发布。
- 数据：`.agent/`、`crud_tests/`、`.cache/` 不在版本控制内，回滚前先导出备份；不要用旧版覆盖新数据目录。
- 镜像：使用上一版本 tag 或 `docker save` 归档恢复。

## 模板与分支

- 当前工作区已有开发时，沿用当前分支并保留未提交改动；不要为开始工作切回旧 tag。只有明确要从已发布基线开始独立的新模板方向时，才从所选 tag 创建新分支，并先确认工作区状态与目标 tag。
- 较大分歧方向使用 GitHub「Use this template」新建仓库；模板复制默认分支内容，不复制 tag 与其他分支。
- 详见 [贡献指南](../CONTRIBUTING.md) 与 [README](../README.md)。
