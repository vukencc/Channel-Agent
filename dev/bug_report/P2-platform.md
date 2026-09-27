# P2 平台化与工程化缺口（先方案后实现）

> 基线提交：`93a7fed`。严重级别与工作流见 [README.md](README.md)。
> 这些条目多数涉及架构选择，修复 Agent 应先给出设计说明与小步验证计划，避免留下半成品。

---

## BUG-17 可观测性不足：last_run 只留最近一轮

**级别** P2　**状态** 已修复（说明见下）　**文件** `core/sessions.py`、`core/log.py`、`core/storage.py`

**症状**
- `last_run` 每次提交被重置，历史运行指标丢失，无法做趋势分析。
- 无 trace/span、无成本面板、审计只覆盖工具调用。

**证据**
- `core/sessions.py:95`：`session.record['last_run'] = {'model_calls': [], 'tools': []}`。
- `core/log.py`：只有基础 logging，无结构化事件。
- `tools/sandbox.py:102-116`：审计仅工具相关事件。

**建议**
1. 增加运行历史（环形缓冲或 JSONL），保留最近 N 轮的模型/工具指标。
2. 引入轻量 trace（JSONL 或 OpenTelemetry 可选），统一 session/turn/tool span。
3. 成本与 token 面板（依赖 BUG-15 的计量）。
4. 审计扩展：读取、网络查询、确认结果。

**验收**
- 可回看最近 N 轮指标；trace 文件可关联到会话与工具。
- 不影响现有性能与日志体量。

---

**修复说明** 低风险实现：core/sessions.py 保存最近 RUN_HISTORY_LIMIT=20 轮指标，分配 turn_id 并关联工具调用 ID；tools/sandbox.py 审计包含 session/turn/tool_call_id；JSON 结构化 turn_finished 诊断事件。不引入 OpenTelemetry 或成本面板。test_bug17_runs.py 验证三轮运行保留两轮及独立追踪 ID。 验证：`uv run pytest dev/tests/test_bug17_runs.py dev/tests/test_sessions.py dev/tests/test_cli_performance.py -q（22 passed）`。

## BUG-18 在途网络/重排/向量调用不可取消

**级别** P2　**状态** 部分修复（协作取消；进程隔离仅设计）　**文件** `tools/web_search.py`、`rag/rerank.py`、`rag/embedding.py`、`core/sessions.py`

**症状**
- 停止会话后，已进入的 web/检索/推理调用仍可能继续占用线程与资源。
- 用户看到「停止中」但会话不能立即回到空闲。

**证据**
- `docs/cli.md`「确认与取消」自述：不能强制杀死 Python 工具线程，RAG/网络工具可能要等当前调用结束。
- `core/sessions.py:178-188`：取消时等待 worker，仅命令进程可被终止。

**建议**
1. 为网络/重排调用引入可中断超时（分片超时、handler 级取消），或在取消后明确阶段与预计等待。
2. 评估独立池化（与 BUG-02 一起）减少相互影响。
3. 文档更新：说明哪些工具不可即时取消，UI 给出更准确的阶段状态。

**验收**
- 取消后会话在有限时间内回到可继续状态；不可取消路径有明确提示。

---

**修复说明** 按 P2 低风险范围实现：rag/cancellation.py 提供协作取消，rag/tool.py 阶段、embedding/rerank 批次间检查；工具超时独立取消标志；CLI 明确等待当前调用退出。已有 web 超时和独立评估池复用。不能强杀在途 native 线程，进程隔离方案与验收见 docs/platform-design.md，未承诺硬截止。test_bug18_cancel.py 验证停止后不进入昂贵索引。无新配置。 验证：`uv run pytest dev/tests/test_bug18_cancel.py dev/tests/test_hybrid_rag.py dev/tests/test_sessions.py dev/tests/test_command.py -q（60 passed）`。

## BUG-19 接口与生态缺口：无 headless/API/MCP/多模态

**级别** P2　**状态** 设计完成（按要求暂不实现）　**文件** `core/cli.py`、`tools/__init__.py`、`rag/index.py`

**症状**
- 只有全屏 CLI，无法作为服务或批处理集成。
- 工具生态封闭，新增工具需要改代码；无 MCP/插件协议。
- 仅文本输入，知识库仅 txt/md，无网页抓取/浏览器/多模态。

**建议（分期）**
1. headless 模式：`--prompt`/`--json` 单次执行，便于脚本与评测。
2. HTTP API + SSE 流式接口；CLI 复用同一核心。
3. MCP 客户端 + 工具清单动态加载 + 按会话启用与权限声明。
4. 工具结果结构化（dict + 渲染），为 MCP/UI 提供统一协议。
5. 可选：网页抓取、浏览器自动化、图片/PDF 输入。

**验收**
- 每个子项独立 PR；headless 与 API 不改变现有 CLI 行为。
- 新工具注册无需修改 `core/` 主循环。

---

**修复说明** 按用户范围仅完成设计：docs/platform-design.md 明确 headless 参数、JSON/JSONL 契约、退出码、默认拒绝审批、配对和持久化验收；API/SSE 的状态锁、幂等/审批，MCP 权限与 schema 预算，多模态上传及网页出网边界。未实现新入口/监听服务、未新增配置/依赖；没有声称通过不存在的运行功能测试。 验证：`设计核对：CLI 默认入口、沙箱确认、会话锁与工具配对契约；无运行代码变更`。

## BUG-20 无 CI、集成测试依赖本地模型与网络

**级别** P2　**状态** 待修复　**文件** `.github/`（不存在）、`pyproject.toml`、`dev/tests/`

**症状**
- 没有持续集成；回归依赖手工命令。
- 集成测试需要本地模型/网络与命名空间权限，无法在普通 CI 直接跑。

**证据**
- 仓库无 `.github/` 目录。
- `pyproject.toml:31-34`：testpaths 与 integration 标记。
- `dev/tests/conftest.py`、命令测试需要 Bubblewrap 命名空间。

**建议**
1. 增加 GitHub Actions：`uv sync --locked` + `uv run pytest -m 'not integration' -q`。
2. 可选 job：缓存本地模型后运行 integration，允许手动触发。
3. 明确命令沙箱测试的环境要求；不支持时输出环境限制说明而不是静默跳过。
4. 评估覆盖率门槛（先只报告，不强制）。

**验收**
- 默认 PR 流程自动跑离线测试；集成任务可手动触发并有缓存策略。
