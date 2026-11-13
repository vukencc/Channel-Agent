# 配置说明

> 文档类型：当前使用指南 · [文档导航](README.md) · [快速上手](getting-started.md)

`.env.example` 是当前完整配置参考模板。源码运行时仅在 `.env` 不存在时复制；Windows 便携成品首次启动自动创建最小 `.env`，请参考数据根中的 `configuration.example.env` 编辑已有文件，不要覆盖凭据。便携路径及选择优先级见 [Windows 指南](windows.md)。进程环境变量优先于 `.env`。修改配置后重启进程生效。

本页只说明应用读取的环境变量。Codex 自身的模型、授权、代理与角色指令由 Codex 配置管理，不是应用 `.env`；不要把 Codex 的角色设置当作应用配置项。

模板按功能分组；除显式保留的 `REASONING_EFFORT=low` 和 `THINKING_MODE=disabled` 服务示例选型外，其余值对应当前代码默认值。布尔项支持大小写形式的 `true`/`false`（模板保留原有 `True`/`False` 写法），JSON 配置项须保持合法 JSON；路径相对项目根目录解析。不要把密钥提交到仓库。

## 配置同步

`uv run python -m agent_test.scripts.sync_env --check` 只检查 `.env` 是否缺少模板键或布局与模板不同：无差异退出码为 0，差异为 1，执行异常为 2。输出仅含统计与键名，不显示配置值。

`uv run python -m agent_test.scripts.sync_env --write` 将本地 `.env` 按模板顺序整理，保留已有赋值的原始写法、注释、引号和变量引用，也保留模板之外的本地键及独有注释；缺少的键采用模板值，已有值优先。它不会把变量引用展开后写回，也不会删除本地自定义值。同步采用原子替换、不跟随符号链接，不创建包含秘密的备份副本；目标文件权限为 `0600`，只读检查不会改权限；目标不存在时会新建。可用 `--template`、`--target` 指定测试路径。完成后重启应用。

重复键、无效语法或重排会改变旧变量引用结果时，同步应拒绝写入；先修正本地引用顺序或环境变量后再运行。`--check` 不修改文件。

运行时模型连接的 `context_tokens` 应按实际服务容量显式填写，不继承其他连接；未声明容量时按未知窗口使用保守输入预算（上限 16000 token）。`response_reserve_tokens` 也是该连接的预算元数据；具体预算关系见[上下文管理](context-management.md)。

`MODEL_CALL_TIMEOUT` 默认 90 秒，限制普通 Agent 模型请求的总时间。Flow 设计助手不使用固定总时限，持续生成的复杂设计可继续等待；保留 `SESSION_TIMEOUT`（默认 60 秒）的网络连接/空闲等待限制、`MAX_RETRIES`、预算及输出校验。停止或清空对话会取消请求；隐藏面板不会取消。此差异按单次请求隔离，不改变同时运行的普通 Agent。

`FLOW_DESIGNER_TOTAL_OUTPUT_CHARS=192000` 为设计助手的独立总生成量上限，统计推理、正文及工具参数，合法范围 1..1048576，禁止 0 或无限值。设计正文和参数仍共同遵守 `MODEL_OUTPUT_CHARS=48000`；普通 Agent 继续以该项限制全部输出。未显式设置模型 `max_tokens` 时，费用预留按有效总字符上限作为保守输出 token 估算，因此增大设计额度可能被已有费用预算拒绝；有 provider usage 时照原规则结算，没有则保留预留。该配置不会修改发给模型的采样参数、推理强度或输出 token 参数。

## 功能索引

- 模型端点、模型名、推理选项、备用模型、费用预算与上下文容量：见模板“模型服务与费用”及“请求、上下文与并发”，以及[结构化历史与上下文管理](context-management.md)。费用是本地估算，不是服务商账单上限。
- 工具许可、文件根目录、命令沙箱、网络访问和资源限额：见[沙箱说明](sandbox.md)与[命令工具说明](cli.md)。授权策略不会取消路径边界、确认流程或审计。
- RAG、嵌入模型、重排器、向量索引和缓存：见[RAG 文档](rag.md)。修改检索阈值前应使用质量集验证。
- 沙箱环境探测与 npm/uv 依赖准备：见[隔离命令环境准备](environment-preparation.md)；安装和显式 `resource_profile="development"` 构建/测试共用开发额度，值为 `0` 时继承通用命令限额。Linux 项目根目录可在会话设置或 CLI `/project` 中选择，无需修改全局 `SANDBOX_DIR`。
- 命令执行环境：`COMMAND_EXECUTION_ENVIRONMENT` 默认 `sandbox`，可设为 `local`；`LOCAL_COMMAND_TIMEOUT` 默认 `600` 秒，范围 `1..86400`。应用配置与 Codex 自身的模型、代理和权限配置分开。Linux 本机模式及其安全边界见[沙箱说明](sandbox.md)。
- 会话、后台命令、Agent 委派和任务计划：见[会话工具](session-tools.md)、[命令模式](cli-command-modes.md)及[任务计划](task-plans.md)。委派保持单层；计划功能默认关闭，启用后由现有 Agent 显式使用工具，不会增加模型调用或自动调度器。
- 工作区共享记忆：`WORKSPACE_MEMORY_ENABLED=true` 默认开启，`WORKSPACE_MEMORY_INJECT_CHARS=6000` 控制最大参考长度；自动提炼使用辅助模型并遵守权限和预算，见[工作区共享记忆](workspace-memory.md)。
- 旧式会话记忆管理、自动候选提取、图片输入与观测功能默认关闭；对应选项见模板分组、[Web UI 文档](web-ui.md)和[Agent 运行调试](debug-runtime.md)。

## 常用安全选项

`TOOL_PERMISSION_POLICY` 默认 `smart`。公开模式为 `smart`、`readonly`、`full_access`；旧会话中保存的 `standard`/`trusted` 仍按原有严格确认行为处理。`full_access` 会自动批准工具确认，但仍受注册开关、工具子集、路径和执行环境规则约束。隔离沙箱中的命令联网默认关闭，开启后仍须使用公网单播白名单；Linux 本机环境直接使用宿主网络。`MODEL_TOOL_NAMES` 为部署级能力上限：`null` 使用框架自动目录，空数组禁用工具，显式列表只收窄能力。经典 Agent 不提供会话工具选择；Flow 节点可在上限内选择工具，旧会话限制及子 Agent 继承仍生效。默认目录隐藏有可用替代接口的兼容名称，见[工具设计](tool-design.md)。

Flow 节点可继承或覆盖应用模型思考模式与推理强度；节点 `disabled` 不发送 reasoning。`MODEL_TOOL_NAMES` 与节点基础工具冲突时会报错。选择和服务商支持边界见 [Agent Flow](agent-flow.md)。这是应用配置，与 Codex 自身配置无关。

`ENABLE_TASK_PLANS` 默认关闭；计划步骤上限默认 32，可配置为 1–256。上下文摘要长度默认 2000 字符，可配置为 256–16000；计划文件上限默认 65536 字节，可配置为 1024–1048576。任务计划记录进度和证据引用，不代表通用自动验收。
