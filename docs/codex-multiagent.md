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

代理定义位于 `.codex/agents/*.toml`。每个定义必须提供 `name`、`description` 和 `developer_instructions`；名称用于选择角色。模型和推理强度只在这些 TOML 文件中维护。角色指令要求只读角色不写文件；这一职责由指令约束，不能当作沙箱级只读保证。代理继承父会话的 provider、沙箱和审批边界，角色配置不能自行提升权限。具名角色的模型和推理强度优先于派生参数及 agents 默认值；要改变设置，应选择其他角色或修改对应文件，不能依靠派生参数临时覆盖。

`.codex/` 和根 `AGENTS.md` 是随仓库提供的项目级默认配置；`docs/dev/` 是不随 clone 分发的本地资料。认证、根模型与权限由调用端管理。此页描述项目约定，官方配置格式以[子代理配置说明](https://learn.chatgpt.com/docs/agent-configuration/subagents)和[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)为准。

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
| `implementation_worker` | 范围明确、文件归属清楚的实现与修复。 |
| `ui_specialist` | 界面复现、视觉和交互修复及实际界面检查。 |
| `architecture_advisor` | 已有针对性证据后，针对未解决的跨模块决策提供只读建议。 |

当前选择将 Terra 用于有界实现、界面和文档，Sol 用于复杂代码、RAG、测试、性能及安全审查，Luna 用于范围明确的只读探查；遇到尚未解决的困难架构决策时按需使用 Astra。这是参考[官方模型定位](https://learn.chatgpt.com/docs/models)和[模型选择指南](https://developers.openai.com/api/docs/guides/model-selection)的工程选择，并非性能或费用基准。角色的实际配置以 TOML 为准，可在仓库根目录只打印角色名、模型和推理强度：

```bash
python3 -c 'import pathlib,tomllib; [(lambda d: print(d["name"], d.get("model"), d.get("model_reasoning_effort")))(tomllib.loads(p.read_text())) for p in sorted(pathlib.Path(".codex/agents").glob("*.toml"))]'
```

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

本轮 WSL Codex CLI 为 `0.160.0`，满足官方文档中 Terra 的 CLI 最低版本 `0.159`；严格诊断返回 `overallStatus=ok`。十个角色 TOML 已解析并检查模型、推理强度及权限覆盖。`codex debug models --bundled` 的离线目录列出 Astra、Sol 6.1 和 Luna，但该旧快照未收录 Terra，不能据此判断服务不可用，也不能将 doctor 结果当作模型授权证明。本轮 `docs_writer` 作为 Terra/medium 子代理实际完成文档工作；三个新增角色尚未逐一真实派生。这些是 Codex 协作验证，不是应用内 Agent 的真实模型测试、保存轨迹回放或受控 fixture 验收。

Windows npm CLI 本轮实测仍为 `0.154.0`，低于 Terra 所需版本；本仓库的 CLI 检查使用 WSL，未升级全局 CLI 或修改桌面应用。

历史验证：先前 WSL 严格诊断返回 `overallStatus=ok`、`config.load=ok` 和 `mcp.config=ok`。当时的 `codex debug prompt-input` 渲染包含项目指令、交接引用和七个角色名称，不包含工具 schema，也未验证角色派生或模型可用性；当时没有发起模型推理请求。

修改配置后先检查当前会话可用角色；宿主若未重新发现，需在同一工作区新开会话再检查。诊断输出可能包含本机配置，不要公开完整配置、认证或 prompt 记录。

相关工作流程：[仓库指南](../AGENTS.md)、[贡献指南](../CONTRIBUTING.md)、[沙箱说明](sandbox.md)、[文档索引](README.md)。
