# 发布与版本冻结

## 版本与标签

- tag 命名 `v<major>.<minor>.<patch>`，使用注解 tag（`git tag -a`）；**tag 一旦推送不再移动**。
- `main` 是模板/稳定分支，受保护，只通过 PR 合入。
- 每个 tag 对应一个可运行基线，变更记录写入 `CHANGELOG.md`。
- 发布镜像由标签工作流构建，镜像标注 `org.opencontainers.image.revision` 关联提交。

## 发布清单

1. `git status` 干净，待发布内容已合入 `main`。
2. `uv sync --locked` 成功。
3. `uv run pytest -m 'not integration' -q` 全绿（当前基线：319 passed, 2 skipped, 5 deselected）。
4. `uv run python dev/ci_sandbox_probe.py` 通过（Linux）。
5. 可选：`RUN_RAG_INTEGRATION=1 RAG_QUALITY_CORPUS=<parquet> uv run pytest -m integration -q`。
6. 更新 `CHANGELOG.md`，提交。
7. 打标签并推送：

   ```bash
   git tag -a vX.Y.Z -m "发布说明摘要"
   git push origin main
   git push origin vX.Y.Z
   ```

8. 标签推送触发 `release.yml`：构建镜像推 GHCR、创建 GitHub Release；检查 Actions 结果。
9. 冷备份：`git bundle create ai-agent-startup-vX.Y.Z.bundle --all`，存放到仓库之外的介质。

## 回滚

- 代码：从目标 tag 开修复分支（`git switch -c fix/xxx vX.Y.Z~` 或对应 tag），修复后按补丁版本发布。
- 数据：`.agent/`、`crud_tests/`、`.cache/` 不在版本控制内，回滚前先导出备份；不要用旧版覆盖新数据目录。
- 镜像：使用上一版本 tag 或 `docker save` 归档恢复。

## 模板与分支

- 新方向从基线 tag 开分支：`git switch -c feat/<topic> vX.Y.Z`。
- 较大分歧方向使用 GitHub「Use this template」新建仓库；模板复制默认分支内容，不复制 tag 与其他分支。
- 详见 [贡献指南](../CONTRIBUTING.md) 与 [README](../README.md)。
