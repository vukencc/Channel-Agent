# 文档索引

文档分为现行使用指南、实现与设计边界、历史交付记录。历史报告保留当时测量和问题描述，不作为当前使用规范；使用命令与配置前请以现行指南和源码为准。新会话接手入口见[会话交接说明](session-handoff.md)。

## 现行使用指南

- [原生桌面 GUI](desktop.md)：不依赖浏览器的窗口、模型设置与会话操作。
- [原生 Agent Flow](agent-flow.md)：画布编辑、项目继承、交接和运行状态。

- [CLI 指南](cli.md)：多会话操作、命令、持久化与确认。
- [CLI 命令与权限模式](cli-command-modes.md)：命令选择器、权限模式和会话回收。
- [会话工具](session-tools.md)：会话创建、子 Agent 和父子通信。
- [长任务执行](long-task-execution.md)：阶段续跑、检查点和子任务等待。
- [任务计划使用说明](task-plans.md)：计划工具、限制与只读查看。
- [本机 WebUI](web-ui.md)：启动、模型管理、并发状态与文件预览。
- [Agent 运行调试](debug-runtime.md)：CLI/WebUI 调试面板、事件与数据边界。
- [配置说明](configuration.md)：环境变量、优先级与本地配置同步。
- [结构化历史与上下文管理](context-management.md)：压缩、归档、检索和监督器配置。
- [沙箱说明](sandbox.md)：文件工具、命令隔离、资源限制与文件操作。
- [隔离命令环境准备](environment-preparation.md)：运行时诊断和项目依赖准备。
- [Docker 指南](docker.md)：镜像构建、运行、数据卷与限制。
- [RAG 指南](rag.md)：模型准备、索引、检索与工程验证。
- [RAG 质量校准](rag-quality.md)：公开 qrels 校准与留出报告。
- [发布说明](release.md)：版本冻结、标签与发布流程。
- [贡献指南](../CONTRIBUTING.md)：开发、验证和 PR 流程。

## 实现与设计边界

- [工具设计与简化](tool-design.md)：模型目录、兼容入口与框架职责。
- [三模块架构](architecture.md)：AgentCore、AgentTest、Frontend 的职责、依赖方向和构建入口。

- [任务状态与计划设计](task-state-design.md)：数据模型、状态约束与实现边界。
- [DEBUG 因果时间线设计](debug-timeline-design.md)：总览、通信、异常、游标和指标语义。
- [平台能力设计](platform-design.md)：跨平台、headless、API/MCP 与容器后端的设计边界。
- [Codex 多代理](codex-multiagent.md)：本地代理配置和协作约定；技术配置以当前 Codex 配置和官方 schema 为准。

## 历史交付与诊断记录

以下材料记录具体时点的实现、问题和实测结果。重构前记录中的路径与命令可能已迁移，历史数字不代表当前性能或当前代码验证结果。

- [DEBUG 总览清晰度报告](debug-overview-clarity-report.md)：既有 trace 回放和受控界面布局测量。
- [DEBUG 时间线交付报告](debug-timeline-delivery.md)：因果事件与界面交付记录。
- [上下文、执行与调试界面交付](context-execution-ui-delivery.md)：预算、长任务与调试布局实现记录。
- [博客故障与真实模型多 Agent 验证](blog-debug-real-model-report.md)：长上下文任务的运行证据和边界。
- [隔离项目验收报告](project-validation.md)：真实模型和监督器的隔离项目验收。
- [工作区优化报告](workspace-optimization.md)：上下文、任务进度和工作区界面实测。
- [上下文 Compact 频率修复](compact-frequency-fix.md)：压缩频率诊断与修复验证。
- [环境检测与依赖准备修复](environment-fix-report.md)：Node 检测与依赖准备问题记录。
- [模型连接诊断与恢复](model-connection-recovery.md)：连接故障、重试和会话恢复记录。
- [CLI 与会话工具交付记录](cli-session-delivery-2026-09-29.md)：多会话 CLI 实现和验收。
- [缺陷修复交付报告](bug-fix-progress.md)：历史缺陷修复过程与验证记录。
- [优化实施记录](optimization-progress.md)：历史优化问题和过程记录。
- [分阶段优化交付](optimization-delivery.md)：历史优化实现和实测。
- [RAG 阶段性能对比](perf-rag-comparison.md)：特定环境下的检索阶段测量。
- [系统诊断报告](system-diagnostics-2026-09-27.md)：历史系统诊断。
- [长文件恢复报告](large-file-recovery.md)：历史长文件工具调用修复记录。
- [RAG 工程交付记录](rag-delivery.md)：早期 RAG 实现记录。
- [RAG 工程测试（2026-09-27）](rag-engineering-2026-09-27.md)：当时的工程集成测试记录。

## 仓库与本地资料

源码分布于 `src/agent_core/`、`src/agent_test/` 和 `src/agent_frontend/`，测试在 `src/agent_test/tests/`，跟踪的 CI 和工程脚本在 `src/agent_test/scripts/`。项目级 Codex 默认配置 `.codex/` 和根 `AGENTS.md` 随仓库提供；本地开发资料 `docs/dev/` 不随 clone 分发。项目启动入口和当前功能简介见[根 README](../README.md)。
`docs/opensource-docker-journey.md` 被 Git 忽略，是仅存在于部分 checkout 的本地记录，不属于可分发文档。
