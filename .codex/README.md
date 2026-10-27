# 项目级 Codex 配置

这些文件配置维护仓库的 Codex，区别于本项目运行时的 Agent、Session 和权限设置。新会话先读根 `AGENTS.md` 与 [交接说明](../docs/session-handoff.md)。`.codex/` 和根 `AGENTS.md` 随仓库提供项目级默认配置；`docs/dev/` 是不随 clone 分发的本地资料。

## 配置范围

`config.toml` 使用当前配置键：

| 设置 | 当前值 | 用途 |
| --- | --- | --- |
| `project_doc_max_bytes` | `16384` | 项目指令读取预算；详细运行状态放在交接文档，避免塞入常驻指令。 |
| `agents.enabled` | `true` | 启用 Codex 子代理工具。 |
| `agents.max_concurrent_threads_per_session` | `3` | 最多三个并发子代理，不包括主代理；实际还受宿主限制。 |

认证、根模型、审批和沙箱由调用端管理，项目配置只维护上述协作参数。旧的 `max_threads` 是兼容别名；本地配置已使用明确的新键，不再依赖旧的 feature 或深度开关。配置层仅在受信任项目中加载，参见 [官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

## 角色文件与协作

`agents/*.toml` 每个文件定义一个角色，必填 `name`、`description`、`developer_instructions`，模型和推理强度也以文件为准。现有角色：`repo_explorer`、`rag_specialist`、`sandbox_auditor`、`test_engineer`、`reviewer`、`docs_writer`、`perf_analyst`、`implementation_worker`、`ui_specialist`、`architecture_advisor`。根 `AGENTS.md` 指定触发条件、文件所有权和返回格式。

采用扁平委派：主代理分配独立任务，子代理向主代理汇报后续工作。共享工作树中每个文件同一时间只有一个写入者，主代理负责整合和最终验证。只读角色通过指令遵守只读职责；独立角色文件不能覆盖父会话的权限或沙箱，不能将 `sandbox_mode` 写在角色文件后宣称获得硬隔离。格式及继承规则见 [官方子代理文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)。

应用内子 Agent 的递归限制由应用后端控制，独立于这里的 Codex 调度；参见 [Session 工具](../docs/session-tools.md)。

## 验证与生效

本机核验使用 WSL 的 Codex CLI `0.160.0`；Windows npm CLI 为 `0.154.0`。在仓库 Linux 路径下执行：

```bash
codex --version
codex --strict-config doctor --json
```

[官方模型说明](https://learn.chatgpt.com/docs/models)要求 Terra 使用 Codex CLI `0.159.0` 或以上。当前 WSL 版本满足要求，Windows npm CLI 尚未满足；本轮没有升级全局 CLI，也没有修改桌面应用的安装或配置。

本次严格诊断返回 `config.load: ok`。它验证配置解析与本地诊断，不等于执行过每个角色的模型调用。`doctor` 的 JSON 已脱敏，交付时仍只保留相关状态；不要将完整用户配置、认证或 prompt 转录贴入报告。`--strict-config features list` 在该版本不受支持，不能作为严格校验替代命令。

角色文件中的模型和推理强度优先于派生参数和 agents 默认值；要改变具名角色的设置，应选择其他角色或修改角色文件。代理继承父会话的 provider、权限与沙箱边界。修改配置后先检查当前可用角色；若宿主尚未重新发现配置，在同一工作区新开会话并核对角色列表和 `AGENTS.md` 是否载入。Windows/WSL 启动方法、Linux uv/Node 路径、工作树状态与最近验证见 [交接说明](../docs/session-handoff.md)；不要用 Windows uv 重建 WSL 的 `.venv`。完整维护约定见 [Codex 协作说明](../docs/codex-multiagent.md)。
