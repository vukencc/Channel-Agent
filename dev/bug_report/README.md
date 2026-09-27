# AI Agent 缺陷与能力上限报告

- 基线提交：`93a7fed`（2026-09-27），分析时工作树干净
- 分析方式：只读静态审查，未改动任何业务代码
- 覆盖范围：`core/`、`tools/`、`rag/`、`config.py`、`dev/tests/`、README 与 docs
- 用途：记录问题、给出修复建议与验收标准，供修复 Agent 逐项处理；修复过程中可直接在本目录更新状态

## 严重级别

| 级别 | 含义 | 处理要求 |
|---|---|---|
| P0 | 会直接导致任务失败、卡死、资源耗尽或安全面扩大 | 优先修复，单个问题单独提交与回归 |
| P1 | 不必然立刻失败，但构成明确的能力或质量天花板 | P0 清理后按序处理 |
| P2 | 平台化与工程化缺口，影响长期演进 | 按路线图排期，先出设计不急于实现 |

## 问题索引

| ID | 标题 | 级别 | 主要涉及文件 |
|---|---|---|---|
| BUG-01 | 上下文预算计量缺口与硬失败 | P0 | `core/context.py`、`core/llm.py`、`core/storage.py`、`core/sessions.py` |
| BUG-02 | 并发信号量被人工确认与后台评估占用 | P0 | `core/sessions.py`、`config.py` |
| BUG-03 | `web_search` 免确认出网、无超时/上限/截断 | P0 | `tools/web_search.py`、`tools/sandbox.py` |
| BUG-04 | 沙箱无 CPU/内存/磁盘/进程数配额 | P0 | `tools/sandbox.py`、`tools/command.py` |
| BUG-05 | 配置缺失或非法在启动时不报错 | P0 | `config.py`、`core/cli.py` |
| BUG-06 | 调试工具常驻生产、DEBUG 输出污染 TUI | P0 | `tools/__init__.py`、`tools/debug.py`、`tools/rag_search.py` |
| BUG-07 | 工程卫生：死代码、文档路径、AUDIT_LOG 语义 | P0 | `core/agent.py`、`core/sessions.py`、`AGENTS.md`、`config.py` |
| BUG-08 | 无 token 计量、无摘要式上下文压缩 | P1 | `core/context.py`、`config.py` |
| BUG-09 | 长输出全有或全无，截断后只能一次性恢复 | P1 | `core/llm.py`、`core/sessions.py` |
| BUG-10 | 保存放大：事件循环 deepcopy + 每步全量重写 | P1 | `core/storage.py`、`core/cli.py` |
| BUG-11 | 工具串行执行、单轮调用无上限、无工具级超时 | P1 | `core/sessions.py`、`tools/base.py` |
| BUG-12 | RAG 每次搜索全量重读语料、仅支持 txt/md | P1 | `rag/index.py` |
| BUG-13 | RAG 阈值未校准、质量回归默认跳过 | P1 | `config.py`、`rag/tool.py`、`dev/tests/test_rag_integration.py` |
| BUG-14 | 记忆仅手工、无上限、全量注入、不可检索 | P1 | `core/storage.py`、`core/sessions.py`、`core/cli.py` |
| BUG-15 | 单模型无 fallback、无 token/成本计量 | P1 | `config.py`、`core/llm.py`、`core/sessions.py` |
| BUG-16 | 评估 prompt 无长度控制 | P1 | `rag/assess.py` |
| BUG-17 | 可观测性不足：last_run 只留最近一轮 | P2 | `core/sessions.py`、`core/log.py` |
| BUG-18 | 在途网络/重排/向量调用不可取消 | P2 | `tools/web_search.py`、`rag/rerank.py`、`rag/embedding.py` |
| BUG-19 | 接口与生态缺口：无 headless/API/MCP/多模态 | P2 | `core/cli.py`、`tools/__init__.py` |
| BUG-20 | 无 CI、集成测试依赖本地模型与网络 | P2 | `.github/`（不存在）、`pyproject.toml`、`dev/tests/` |

## 状态

BUG-01 至 BUG-17 已完成本轮修复（P1 重大改动采用文档化最小版本）；BUG-18 已实现协作取消，硬截止进程隔离仅设计；BUG-19 按要求仅设计；BUG-20 CI 基础已实现、本机验证，远端运行未触发。逐项说明与验证见 [修复记录](../../docs/bug-fix-progress.md)。

## 修复工作流要求

1. 顺序：P0（BUG-01…07）→ P1（BUG-08…16）→ P2（先方案后实现）。
2. 每个 BUG 一个独立提交，提交信息说明行为变化与验证命令。
3. 每个修复先补一个能复现问题的失败测试，再实现；不要删除或弱化现有测试。
4. 行为、配置、限制变化必须同步更新 `.env.example` 与 `docs/`。
5. 不得修改或删除用户数据：`.agent/`、`crud_tests/`、`data/raw/`、`logs/`、`.cache/`。
6. 不得为通过测试放宽沙箱、确认、审计或工具调用配对校验。
7. P2 中涉及架构改动（存储重构、MCP、API 服务）的条目，先给出设计说明，不要留半成品。

## 验收基线

```bash
uv sync --locked
uv run pytest -m 'not integration' -q        # 必须全绿
# 针对性回归示例
uv run pytest dev/tests/test_context.py dev/tests/test_sessions.py -q
```

可选（需要本地模型或真实 API 配额，默认不跑）：

```bash
RUN_RAG_INTEGRATION=1 uv run pytest -m integration -k 'not strictness'
uv run python -m dev.diagnose --live --runs 1
```

命令沙箱测试需要环境允许创建 Linux 命名空间（Bubblewrap）；无法创建时应如实报告环境限制，不得把环境失败误记为应用通过或跳过。
