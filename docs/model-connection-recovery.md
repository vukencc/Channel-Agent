> 历史报告，不作为当前使用规范；保留当时的问题描述和实测结果。当前说明见[当前文档索引](README.md)。

# 模型连接诊断与恢复记录（2026-10-07）

## 现象与复验

会话 `213c208ecbdc4ec6b2c59d550607c8eb` 的首个模型请求耗时 40.966 秒后以 `APIConnectionError: Connection error.` 结束，输出为零、工具执行为零。上下文预算利用率约 76.7%，没有归档或压缩。`insufficient_history` 仅表示尚无足够历史来调用监督器，不是监督模型给出的溢出判定，也不是本次连接失败的原因。

随后在相同配置下进行网络探测：目标 TCP 连接约 0.008 秒；模型服务的 `GET /models` 返回 HTTP 200，用时约 1.147 秒。读取该会话的 `messages.jsonl`，在不执行工具、不写回原会话的隔离探测中重发其当前请求，本次 3.304 秒成功收到 89 个文本字符及 `list_files`、`run_command` 两个完整工具调用；服务商 usage 为输入 8,272、输出 138 tokens。

这次复验说明当时请求可以成功，原连接故障未能持续复现；单次成功不能证明长任务稳定，也不能确认故障来自代理、WSL、网络路径或模型网关。没有证据支持将故障归因于其中任何一项。

## 实现边界与诊断

当前实现将 `MAX_RETRIES` 作为首个流帧之前的总尝试次数（包含首次请求）；它覆盖请求创建和等待首个 SSE 帧。默认值为 2。首帧后的连接中断不会自动重放。网络连接错误会关闭流、结算本次预算并将会话保存为 `model_connection` checkpoint；用户明确发送后续消息后，Agent 才从保存的历史继续。已经完成的工具不自动重放。`MODEL_CALL_TIMEOUT` 默认 90 秒，约束整个模型调用；`SESSION_TIMEOUT` 默认 60 秒，传给 HTTP 客户端作为请求超时。

`model_metrics` 记录连接错误类别、`failure_phase`（`connect` 或 `stream`）、底层传输错误类别及 `request_attempts`。错误正文不会写入连接诊断日志或会话错误字段；会话消息显示安全的连接中断和阶段保存提示。会话最新一轮信息保存在 `session.json` 的 `last_run`。字符数不等于 token 数。

修复后原任务首轮请求再次成功，3.238 秒收到完整 `list_files`/`run_command` 调用，`request_attempts=1`，仍未执行工具或写回原会话。这个请求使用保存的原始消息及当前工具定义，没有重建原运行的全部动态上下文，所以是首轮连接复验，不是完全相同请求的受控性能比较。修复处理的是首帧前重试缺口、错误分类和进度恢复；原始连接故障的具体传输原因没有被旧记录保存，无法追溯确定，外部服务仍可能掉线。

## 失败测试与验证结果

新增 `tests/test_model_connection.py`：首帧前断连重试、首帧后不重放部分工具、错误类别/底层原因脱敏、实际写文件后 checkpoint 与显式继续；这四项先失败，修复后通过。另补首帧等待取消/总超时、逐请求预算结算和空 metadata 帧也禁止重放。WebUI 的两项旧连接错误提示测试先失败，现给出固定中文服务/网络/代理检查及继续提示，不泄露异常原文。

| 验证 | 结果 |
| --- | --- |
| `uv sync --locked` | 通过，无依赖或锁文件改动。 |
| 默认关闭可选功能的完整非 integration 回归，安装已有 web extra | 783 passed、2 skipped、5 deselected，22.35 秒。跳过为可选 ANN，排除为 integration。 |
| 结构化上下文显式开启，结构化会话/边界/连接/Web 状态四个测试文件 | 53 passed，4.36 秒。 |
| 连接/Web/会话/LLM 重试与 fallback 针对性回归 | 46 passed，4.50 秒。 |
| 真实 Bubblewrap + prlimit 沙箱探测 | 通过，探测审计指向系统临时目录。 |
| `git diff --check` 与只读代码复核 | 通过；取消、预算及 metadata 帧覆盖已补齐。 |

```bash
env ENABLE_STRUCTURED_CONTEXT=false ENABLE_TASK_PLANS=false uv run --extra web pytest -m 'not integration' -q
env ENABLE_STRUCTURED_CONTEXT=true ENABLE_TASK_PLANS=false uv run --extra web pytest tests/test_structured_sessions.py tests/test_context_boundaries.py tests/test_model_connection.py tests/test_web_context_status.py -q
AUDIT_LOG=/tmp/agent-connection-sandbox-audit.jsonl uv run python dev/ci_sandbox_probe.py
```

本轮没有新增配置；`.env.example` 只补充原有 `MAX_RETRIES` 的语义注释，实际 `.env` 未修改。原故障会话的 session.json、messages.jsonl、audit.jsonl，以及生命周期日志和 cli.log，大小、时间戳与 SHA-256 均保持不变。一次全套回归发现旧测试向主 `logs/audit.log` 追加 7 条测试事件、1487 字节；原 29022 字节前缀哈希不变，没有删除或回滚日志。随后补充 `tests/conftest.py` 自动临时审计夹具（包括子进程环境），新增失败测试验证未设置工具上下文时也不能写主日志。修复后的完整回归不再追加主日志。

重启 CLI/WebUI 后，可在原故障会话发送“继续”；不需要删除会话或清空缓存。历史错误记录保留，新的连接失败会以可继续的 checkpoint 保存。

真实模型诊断命令为：

```bash
uv run python -m scripts.diagnose --live --runs 3 --thinking disabled --read-timeout 60
```

该命令会消耗当前 API 配额，在临时隔离工作区运行最多 3 组模型任务，并自动批准测试中的确认操作；测试产物写入忽略目录 `.cache/reports/system-diagnostics/live/`。不要把密钥、完整请求 URL 或私有会话内容粘贴到问题报告中。上面的受控单次探测结果不等同于该多轮工程诊断，也不代表完整大型任务验收通过。
