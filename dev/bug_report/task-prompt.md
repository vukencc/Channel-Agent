# 修复 Agent 任务提示词（可直接复制给另一个 Agent 进程）

```text
你是一个在 /home/elaine_vuken/projects/ai-agent-startup 仓库工作的修复 Agent。请按 dev/bug_report/ 中的报告修复缺陷与能力上限。

## 项目背景
这是一个 Python 3.12 的流式 ReAct Agent 实验项目：多会话全屏 CLI（prompt_toolkit）、工具注册表、
Bubblewrap 命令沙箱、混合 RAG（向量 + BM25 + RRF + CrossEncoder）、JSON 会话持久化与审计。
基线提交为 93a7fed。主要目录：core/（Agent 循环、模型调用、存储）、tools/（工具）、rag/（检索）、
dev/tests/（pytest 测试）、docs/（文档）。

## 先读这些（不要跳过）
1. AGENTS.md：仓库约定、测试与提交规范。
2. README.md 与 docs/cli.md、docs/sandbox.md、docs/rag.md。
3. dev/bug_report/README.md（问题索引与工作流）、P0-blockers.md、P1-ceilings.md、P2-platform.md。

## 目标
1. 修复 P0-blockers.md 中的 BUG-01 到 BUG-07，全部完成。
2. 按顺序处理 P1-ceilings.md 中的 BUG-08 到 BUG-16；若某项需要较大架构改动（如存储重构），先实现可验证的最小版本，不要留半成品。
3. P2-platform.md 只做低风险项（如 CI 基础、headless 模式设计），涉及 API/MCP/多模态的先写设计说明，不强行实现。

## 硬性约束
- 不得修改或删除用户数据：.agent/、crud_tests/、data/raw/、logs/、.cache/。
- 不得提交 .env 或任何密钥；新增配置写入 .env.example。
- 保持现有安全语义：沙箱 fail-closed、写操作确认、原子写、审计日志、工具调用与结果配对校验。不得为通过测试而放宽它们。
- 不删除、不弱化现有测试。每个修复先写一个能复现问题的失败测试，再实现。
- 遵循现有风格：4 空格缩进、snake_case、Pydantic 参数模型 + register_tool、中文注释与文档。
- 依赖变更需同步 pyproject.toml 与 uv.lock（使用 uv lock / uv sync --locked），不要手动编辑锁文件。
- 行为、配置、限制变化必须同步更新 .env.example 与 docs/。
- 不得硬编码模型回答、不得伪造工具结果或测试输出。
- 不修改 dev/bug_report/ 中的问题描述，只允许更新每项状态与补充修复说明。

## 工作流
1. 先执行 git status 与 git log --oneline -5，确认工作树状态；如存在用户未提交改动，先判断是否相关，不要覆盖。
2. 建立任务清单：每个 BUG 一个 TODO。
3. 对每个 BUG：阅读报告中的证据行号 → 复现/补失败测试 → 最小实现 → 运行对应测试 → 更新文档与 .env.example。
4. 每个 BUG 一个独立 commit，前缀 fix: 或 feat:，提交信息说明行为变化与验证命令。
5. 小步提交，避免一次改动多个不相关模块；每个提交后工作树应可回归。

## 验证要求
必跑：
  uv sync --locked
  uv run pytest -m 'not integration' -q
针对性回归示例：
  uv run pytest dev/tests/test_context.py dev/tests/test_sessions.py -q
  uv run pytest dev/tests/test_web_search.py -q   # 若新增
可选（需要本地模型或真实 API 配额，默认不跑，除非环境已具备且用户允许）：
  RUN_RAG_INTEGRATION=1 uv run pytest -m integration -k 'not strictness'
  uv run python -m dev.diagnose --live --runs 1
命令沙箱测试需要环境允许创建 Linux 命名空间（Bubblewrap）。若环境不允许，如实报告受限原因，
不得把环境失败记作通过，也不得为了绕过而放宽隔离。

## 交付格式
结束时输出一份简报：
- 已修复的 BUG ID 与对应 commit hash（一行一项）。
- 每项的根因、改动文件、新增测试、验证命令与结果。
- 未完成项与原因（环境限制、需要产品决策、风险过高）。
- 新增配置项列表（名称、默认值、含义）。
- 遗留风险与建议的下一步。
```
