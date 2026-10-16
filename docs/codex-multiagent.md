# Codex 多代理协作

本文记录本机 Codex 配置和仓库内的代理分工。Codex 子代理通过 `spawn_agent` 创建，使用 `followup_task` 追加工作、`wait_agent` 等待结果，并可通过 `send_message` 交换信息。本仓库要求子代理向父代理报告，不继续派生代理。应用本身的 Agent/子 Agent 是另一套运行时功能，不能与 Codex 子代理混为一谈；应用委派边界见[会话工具](session-tools.md)。

## 本地配置

`.codex/config.toml` 的当前项目配置为：

```toml
project_doc_max_bytes = 16384

[agents]
enabled = true
max_concurrent_threads_per_session = 3
```

`max_concurrent_threads_per_session` 限制同一会话中的已派生代理，不计主代理。Codex 配置解析可能接受兼容别名，但本项目以 `max_concurrent_threads_per_session` 为准。`project_doc_max_bytes` 控制项目指令文档的读取预算。

代理定义位于 `.codex/agents/*.toml`。每个定义必须提供 `name`、`description` 和 `developer_instructions`；名称用于选择角色。模型和推理强度只在这些 TOML 文件中维护。角色指令要求只读角色不写文件；这一职责由指令约束，不能当作沙箱级只读保证。代理继承父会话的沙箱和审批边界，角色配置不能自行提升权限。

`.codex/`、根 `AGENTS.md` 和 `dev/` 是本地资产，不随 clone 分发；当前 checkout 可读取它们，fresh clone 不一定包含。此页描述本机约定，官方配置格式以[子代理配置说明](https://learn.chatgpt.com/docs/agent-configuration/subagents)和[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)为准。

## 角色分工

调度触发条件和责任分工以根 `AGENTS.md` 的 Codex Delegation 部分为准；模型与推理强度以 `.codex/agents/*.toml` 为准，本文不重复维护。

| 角色 | 负责范围 |
| --- | --- |
| `repo_explorer` | 只读代码探查、调用路径与文件行证据。 |
| `rag_specialist` | RAG 管线、索引、检索质量与延迟。 |
| `sandbox_auditor` | 只读沙箱、权限、命令/文件/网络边界和审计检查。 |
| `test_engineer` | pytest 覆盖、失败测试诊断与修复。 |
| `reviewer` | 实现完成后的只读缺陷优先评审。 |
| `docs_writer` | 文档、README、示例配置和更新日志同步。 |
| `perf_analyst` | RAG、沙箱或 CLI 的可复现实测。 |

## 委派与共享工作区

按根 `AGENTS.md` 的 Codex Delegation 规则，对可独立开展的实质任务主动委派；小型、紧密耦合或依赖前序结果的工作留在主线程。独立只读探查可以并行；写入任务要划分不同文件责任，避免同时编辑同一文件。所有代理共享当前 checkout，修改会立即互相可见。开始前检查 `git status --short`，保留已有更改；不要用 reset、clean 或切换旧 tag 来整理共享工作区。子代理完成后由父代理整合并负责最终验证。

`fork_turns` 使用 `"none"` 或 `"all"`。默认从 `"none"` 开始并提供具体任务范围；只有子任务确实需要当前对话时才传 `"all"`。不要传整数。Codex 子代理可以继续创建其他代理，但本项目工作流要求子代理把独立后续需求交回父代理，不自行继续派发。

只读角色的文件修改限制来自角色指令；有效沙箱和审批由父会话及运行环境决定。不要仅凭角色名称或角色文件中的描述推断硬隔离。

## 验证

从仓库根目录检查本地 TOML 语法：

```bash
python -c "import tomllib,glob; [tomllib.load(open(p,'rb')) for p in glob.glob('.codex/**/*.toml', recursive=True)]"
```

Codex CLI 当前支持的严格配置检查命令为：

```bash
codex --strict-config doctor --json
```

这会验证 CLI 配置并输出诊断 JSON；它不单独证明每个自定义角色已成功启动。继续验证角色时，应在 Codex 会话中按角色明确请求一项小型、只读的探查，并检查交回的结果和共享工作区状态。

在 WSL 环境中，`codex --strict-config doctor --json` 已返回 `overallStatus=ok`、`config.load=ok` 和 `mcp.config=ok`。`codex debug prompt-input 'Verify repository instructions only.'` 可检查项目指令是否被提示词加载；该命令不接受 `--strict-config`。已验证的渲染包含 `AGENTS.md` 的关键约定、handoff 引用和七个角色名称，但不包含工具 schema，也不验证角色是否能成功派生或各自模型是否可用。本轮没有发起模型推理请求。

相关工作流程：[仓库指南](../AGENTS.md)、[贡献指南](../CONTRIBUTING.md)、[沙箱说明](sandbox.md)、[文档索引](README.md)。
