# 项目阶段状态（2026-11-03）

> 文档类型：阶段快照（2026-11-03） · [文档导航](README.md) · [快速上手](getting-started.md)

本页记录该日期的交付和验证范围，不是持续运行的状态仪表盘。后续变更以源码、当前指南及[会话交接](session-handoff.md)为准。

## 已交付

原生 GUI、WebUI 与 CLI 提供经典 Agent 会话；原生和 Web 提供 Flow 画布、模板、项目、设计助手、节点模型与工具设置，以及便携模板导入导出。Flow 模板和项目独立：模板无需项目即可编辑，运行须选择项目及有效工作区；各节点独立会话共享项目，按拓扑串行执行。设计助手只设计，不执行节点。节点工具选择受部署上限约束，基础 `read_file` 与 `list_files` 锁定。详见 [Agent Flow](agent-flow.md)。

Core 提供工具权限、确认、审计、命令环境、结构化上下文和混合 RAG；能力与使用条件见[架构](architecture.md)、[沙箱](sandbox.md)和 [RAG](rag.md)。Codex 的多代理配置是开发协作配置，与应用内子 Agent 不同，见 [Codex 多代理](codex-multiagent.md)。

## 验证与边界

- 本次阶段整理在最新主分支锁定依赖上重跑非集成回归：**1823 passed、2 skipped、5 deselected，144.34 秒**；真实 Bubblewrap/prlimit 探针通过。两项跳过为未安装可选 ANN extra，integration 用例未启用；本次未重新运行付费模型场景。此前功能验收及其不同耗时保留在[原记录](agent-flow-designer-customer-validation-2026-11-03.md)。
- 三个模块 wheel 离线构建成功，161 份源码/资源逐份哈希匹配；68 份 Markdown 的 413 个本地链接和 16 份 TOML/锁配置检查通过。可选 RAG 质量回归的旧模块路径先复现失败再修正，正确入口 `--help` 及该文件的 4 项离线测试通过；未冒称重新完成真实 RAG 质量测量。
- 真实设计助手与客服五节点第 05 轮均显示 completed，但原业务静态验收为 **562 通过、1 失败**。后续真实服务与人工定向回归，经人工反馈补充后，14 条服务和 6 条人工结果通过独立语义复核；这不是五节点重新鲜跑全绿。[完整记录](agent-flow-designer-customer-validation-2026-11-03.md)
- 设备借用业务场景曾进行真实五节点执行、独立应用验收和浏览器检查；结果属于该次输入和环境，详见[设备借用验证](agent-flow-equipment-validation-2026-10-31.md)。早期客服流程的质量缺口保留在[客服实测](agent-flow-customer-service-validation-2026-11-02.md)。
- [Flow 创作验证](agent-flow-authoring-validation-2026-11-02.md)区分真实模型设计、受控模型界面检查和实际浏览器；历史调试报告也可能使用保存的 trace 回放或受控 fixture，不能当作新的真实模型执行。

Flow 的分支目前串行调度，不提供框架级条件路由；节点职责提示不是文件权限隔离。模型输出、引用和业务产物需要独立核验。RAG 阈值及质量范围见 [RAG 质量校准](rag-quality.md)；具体报告中的历史耗时不代表当前性能。
