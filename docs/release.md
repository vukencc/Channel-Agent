# 发布与版本冻结

## 版本与标签

- tag 命名 `v<major>.<minor>.<patch>`，使用注解 tag（`git tag -a`）；**tag 一旦推送不再移动**。
- `main` 是模板/稳定分支，受保护，只通过 PR 合入。
- 每个 tag 对应一个可运行基线，变更记录写入 `CHANGELOG.md`。
- 发布版本先更新 `pyproject.toml`、`uv.lock` 与 `CHANGELOG.md`，经 PR 合入 `main`；tag 版本须与包版本及变更记录标题一致。
- 标签工作流要求 tag 为 `vN.N.N` 格式、注解 tag，且目标提交已位于 `origin/main`。已推送的 tag 不移动。
- 发布镜像由标签工作流构建，镜像标注 `org.opencontainers.image.revision` 关联提交。

## 发布清单

1. 更新 `pyproject.toml` 和 `uv.lock` 中的包版本，并在 `CHANGELOG.md` 添加对应版本标题及内容；通过 PR 合入 `main`，不要直接 push `main`。
2. `git fetch origin`，再快进本地 `main` 到 `origin/main`，确认工作区干净：

   ```bash
   git fetch origin
   git switch main
   git merge --ff-only origin/main
   git status --short
   ```

3. 确认版本号严格匹配 `vN.N.N`，且与 `pyproject.toml`、`uv.lock` 的包版本和 `CHANGELOG.md` 对应标题一致；工作流还会检查注解 tag 及提交是否位于 `origin/main`。
4. `uv sync --locked` 成功。
5. `uv run pytest -m 'not integration' -q` 全绿。
6. `uv run python scripts/ci_sandbox_probe.py` 通过（Linux）。
7. 可选：`RUN_RAG_INTEGRATION=1 RAG_QUALITY_CORPUS=<parquet> uv run pytest -m integration -q`。
8. 创建并推送注解 tag；不要直接推送 `main`：

   ```bash
   git tag -a vX.Y.Z -m "发布说明摘要"
   git push origin vX.Y.Z
   ```

9. 标签推送触发 `release.yml`：构建镜像推 GHCR、创建 GitHub Release；检查 Actions 结果。
10. 冷备份：`git bundle create ai-agent-startup-vX.Y.Z.bundle --all`，存放到仓库之外的介质。

## 回滚

- 代码：从目标 tag 开修复分支（`git switch -c fix/xxx vX.Y.Z~` 或对应 tag），修复后按补丁版本发布。
- 数据：`.agent/`、`crud_tests/`、`.cache/` 不在版本控制内，回滚前先导出备份；不要用旧版覆盖新数据目录。
- 镜像：使用上一版本 tag 或 `docker save` 归档恢复。

## 模板与分支

- 新方向从当前基线 tag 开分支：`git switch -c feat/<topic> v0.2.1`。
- 较大分歧方向使用 GitHub「Use this template」新建仓库；模板复制默认分支内容，不复制 tag 与其他分支。
- 详见 [贡献指南](../CONTRIBUTING.md) 与 [README](../README.md)。
