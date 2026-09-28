# 更新日志

格式参考 Keep a Changelog；版本号遵循语义化版本；每个 tag 对应一个可运行基线。

## [Unreleased]

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

[Unreleased]: https://github.com/vukencc/ai-agent-startup/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/vukencc/ai-agent-startup/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/vukencc/ai-agent-startup/releases/tag/v0.1.0
