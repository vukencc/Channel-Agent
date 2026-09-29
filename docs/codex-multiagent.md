# Codex 多代理（subagent）协作系统

本文说明本仓库为 OpenAI Codex CLI 设计的子代理系统：它由哪些文件组成、Codex 在什么
情况下启动哪个子代理、并行与写入冲突如何处理，以及如何扩展。目标读者是维护者和
Codex 本身（`AGENTS.md` 中的路由规则是 Codex 读取的调度策略）。

## 背景与目标

Codex 支持多代理工作流：主代理（`/root`）可以用 `spawn_agent` 创建子代理，用
`followup_task` 追加任务，用 `wait_agent` 等待结果，用 `send_message` 传递消息。子代理
各自消耗模型与工具额度，因此**只在任务可拆分、可并行时使用**。

关键约束（Codex 当前行为）：

- **必须显式请求才会 spawn。** 系统提示中的 `<multi_agent_mode>` 明确说明：除非用户或
  适用的 `AGENTS.md`/skill 指令明确要求委派或并行代理，否则不要启动子代理。因此本项目把
  调度策略写进根 `AGENTS.md`，作为常驻的显式指令。
- **并发槽位有限。** 当前构建提供 4 个并发槽位（含根代理），并建议批量不超过 3 个 worker。
- **共享工作区。** 所有代理使用同一个容器、同一个工作目录；任一代理的改动立即对其他代理
  可见，因此并行写入同一文件会产生冲突。
- **模型/推理强度继承规则。** 完整历史 fork（`fork_turns` 省略或为 `"all"`）会继承父
  代理的模型与推理强度，且不接受覆盖；只有在显式指定 `model`/`reasoning_effort` 时才应
  把 `fork_turns` 设为 `"none"` 或正整数。
- **`max_depth` 默认 1。** 子代理可以再 spawn，但本系统设计为扁平委派，不依赖多层嵌套。

## 文件结构

```text
.codex/
  config.toml                 # [agents] 限制、multi_agent 开关、AGENTS.md 字节预算
  agents/
    repo-explorer.toml        # repo_explorer：只读代码探查
    rag-specialist.toml       # rag_specialist：RAG 管线实现
    sandbox-auditor.toml      # sandbox_auditor：只读安全/边界审计
    test-engineer.toml        # test_engineer：pytest 覆盖
    reviewer.toml             # reviewer：只读缺陷优先评审
    docs-writer.toml          # docs_writer：文档同步
    perf-analyst.toml         # perf_analyst：性能测量
AGENTS.md                     # “Multi-agent Workflow” 章节：调度策略（Codex 读取）
docs/codex-multiagent.md      # 本文
```

每个 `.codex/agents/*.toml` 定义一个子代理，必填字段为 `name`、`description`、
`developer_instructions`；可选 `nickname_candidates`、`model`、
`model_reasoning_effort`、`sandbox_mode`、`mcp_servers`、`skills.config`。Codex 以
`name` 字段为准识别代理，文件名只用于区分。

只读代理显式声明 `sandbox_mode = "read-only"`，作为硬限制；写入类代理省略
`sandbox_mode`，继承父会话沙箱，避免绕过用户当前的沙箱与审批策略。

## 角色与触发条件

| 子代理 | 模型 / 强度 | 沙箱 | 触发条件 |
| --- | --- | --- | --- |
| `repo_explorer` | gpt-6-luna / low | read-only | 需要定位代码、追踪调用路径，或在改动前收集 `file:line` 证据 |
| `rag_specialist` | gpt-6-sol / high | 继承 | 改动涉及 `src/ai_agent_startup/rag/**`、`tools/rag_search.py`，或检索质量、索引、RAG 延迟 |
| `sandbox_auditor` | gpt-6-astra / high | read-only | 改动涉及沙箱/命令/文件/网络工具、权限、审计、预算，或需要边界安全评审 |
| `test_engineer` | gpt-6-sol / medium | 继承 | 行为变更需要回归测试，或失败测试需要定位并修复 |
| `reviewer` | gpt-6-astra / high | read-only | 实现完成、准备提 PR，需要对相对基线的 diff 做缺陷优先评审 |
| `docs_writer` | gpt-6-luna / medium | 继承 | 文档、`.env.example`、`README.md`、`CONTRIBUTING.md`、`CHANGELOG.md` 需与代码同步 |
| `perf_analyst` | gpt-6-luna / medium | 继承 | 需要性能改动的前后对比数据（RAG、沙箱、CLI） |

模型选择依据本地模型目录：`gpt-6-astra` 面向最困难的工作（评审、安全审计用高强度），
`gpt-6-sol` 是编码主力（实现类任务），`gpt-6-luna` 快且经济（探查、文档、测量）。

## 调度规则

### 应该委派

- **读多而独立**：并行探查、日志/测试输出分析、跨目录定位。
- **领域隔离**：单个改动集中在一个领域（RAG、沙箱、文档）且可由一个子代理独立完成。
- **可并行的独立子任务**：每个子任务有明确问题与预期产出，例如按维度拆分代码评审。

### 不应该委派

- 几步工具调用就能完成的小改动、单文件且有明显修法的改动。
- 依赖上一步结果的串行推理（留在主线程）。
- 需要当前主线程已有上下文的工作。

### 并行与写入

- 独立只读任务并行；每批不超过 3 个 worker（并发槽位含根代理共 4 个）。
- 所有代理共享工作树，**同一文件/目录同一时间只允许一个写入者**；写操作必须串行。
- 只读代理（`repo_explorer`、`sandbox_auditor`、`reviewer`）可以安全并行。

### 上下文传递

- 默认用 `fork_turns = "none"`（或较小整数）让子代理从干净上下文开始，避免上下文污染，
  并让 TOML 中固定的模型/强度生效。
- 仅当子代理确实需要父会话完整历史时才传全量（此时模型与强度会继承父级，不能覆盖）。

## 输出契约

子代理返回**提炼后的结论**，而不是原始转录：

- 探查/审计：`path/to/file.py:line` + 一行相关性说明，按重要性排序；结论先行。
- 实现：改动文件、运行过的命令与测试、最终状态、残余风险。
- 评审：先列发现（`[P0]`–`[P3]`），再给总体评估与测试缺口。

子代理改完代码后，**最终验证由主线程负责**：运行
`uv run pytest -m 'not integration' -q`；若涉及工具或沙箱边界，再运行
`uv run python dev/ci_sandbox_probe.py`。

## 兼容与回退

若某个 Codex 运行时无法按名字解析 `.codex/agents/*.toml` 中的自定义代理（部分工具化
/API 会话存在该限制），则回退为：spawn 内置 `worker`（只读任务用 `explorer`），并把
对应 `.codex/agents/<name>.toml` 的 `developer_instructions` 原文拼进任务描述。这样
即使命名代理不可用，也能保持相同的角色指令与产出契约。

## 扩展方式

1. 新建 `.codex/agents/<name>.toml`，包含 `name`、`description`、
   `developer_instructions`，按需设置 `model`、`model_reasoning_effort`、
   `sandbox_mode`、`nickname_candidates`。
2. 在根 `AGENTS.md` 的 Roster 表中新增一行，写清触发条件与产出。
3. 如新增领域约束，在 `developer_instructions` 中引用具体路径与验证命令，保持与
   `AGENTS.md`、`CONTRIBUTING.md` 一致。
4. 不要为同一职责创建多个重叠代理，避免调度歧义。

## 验证

- 校验 TOML 语法：`python -c "import tomllib,glob; [tomllib.load(open(p,'rb')) for p in glob.glob('.codex/**/*.toml', recursive=True)]"`。
- 确认 `AGENTS.md` 会被读取：在仓库根运行
  `codex debug prompt-input "probe"`，检查输出中是否包含 “Multi-agent Workflow” 章节。
- 确认配置可加载：`codex doctor --summary --no-color`（Configuration 一节应显示 `config loaded`）。
- 实际调度验证：在 Codex 会话中要求“并行探查 X、Y”，观察 `/agent` 面板中出现的子代理
  名称与角色。

## 相关文档

- 仓库约定与调度策略：[AGENTS.md](../AGENTS.md)
- 贡献流程：[CONTRIBUTING.md](../CONTRIBUTING.md)
- 沙箱语义：[sandbox.md](sandbox.md)
- RAG 工程与验证：[rag.md](rag.md)
