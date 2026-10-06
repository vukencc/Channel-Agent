# 文档索引

## 使用文档

- [CLI 指南](cli.md)：多会话操作、命令、持久化与确认。
- [CLI 命令与权限模式](cli-command-modes.md)：命令选择器、风险确认模式与会话回收。
- [会话工具](session-tools.md)：会话创建、子 Agent 和父子通信。
- [任务计划使用说明](task-plans.md)：启用计划工具、维护步骤与查看 CLI/Web 状态。
- [任务状态与计划设计](task-state-design.md)：实现边界、数据模型和后续阶段方案。
- [本机 WebUI](web-ui.md)：本地浏览器管理、配置、并发状态与文件预览边界。
- [沙箱说明](sandbox.md)：文件工具、命令隔离、资源限制与长文件编辑。
- [Docker 指南](docker.md)：构建、运行、模型缓存、命令沙箱权限与镜像发布。
- [RAG 文档](rag.md)：模型准备、索引、检索与工程验证流程。
- [RAG 质量校准](rag-quality.md)：公开 qrels 小候选池的阈值校准与留出报告。
- [平台设计](platform-design.md)：headless/API/MCP/多模态与容器后端的边界契约。
- [Codex 多代理系统](codex-multiagent.md)：子代理角色、触发条件、并行与写入策略、扩展与验证。
- [发布说明](release.md)：版本冻结、标签与发布清单。

## 交付与过程记录（重构前布局）

> 以下报告是 2026-09 的历史记录，保留原文与当时的实测数据；其中的路径
> （`core/`、`tools/`、`rag/`、`dev/tests/`、`main.py`）指重构前布局。
> 当前布局为 `src/ai_agent_startup/`、`tests/`、`docker/`，见 [README](../README.md)。

- [缺陷修复交付](bug-fix-progress.md)
- [优化进度记录](optimization-progress.md)
- [优化交付报告](optimization-delivery.md)
- [RAG 四阶段对比](perf-rag-comparison.md)
- [系统诊断报告](system-diagnostics-2026-09-27.md)
- [长文件恢复报告](large-file-recovery.md)
- [RAG 交付记录](rag-delivery.md)
- [RAG 工程测试](rag-engineering-2026-09-27.md)
