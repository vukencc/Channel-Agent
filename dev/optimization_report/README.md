# Agent 性能与自由度优化报告

- 基线提交：`7e7499b`（2026-09-28），工作树干净
- 目的：盘点当前代码中的硬编码、单值配置与工作范围限制，作为性能优化与能力开放的任务来源
- 原则：**默认行为与安全语义不变**；新增能力一律显式配置、可审计、有测试；性能优化必须有前后对比数据
- 修复报告见 `dev/bug_report/`（已验收），本目录只处理「优化/开放」，不重复已修复缺陷

## 目录

| 文件 | 内容 |
|---|---|
| `performance.md` | PERF-01 ~ PERF-14：性能瓶颈与可测量优化项 |
| `freedom.md` | FREE-01 ~ FREE-16：硬编码/范围限制与能力开放项 |
| `task-prompt.md` | 给执行 Agent 的完整提示词，可直接复制 |

## 优先级

| 阶段 | 条目 | 说明 |
|---|---|---|
| P0（1~3 天，低风险高收益） | PERF-05/06/07/08/12、FREE-03/05/13/16 | 不改架构，主要是懒加载、去重复计算、节流、参数化、补文件工具 |
| P1（1~2 周，核心优化） | PERF-01/02/03/04/09/10/11、FREE-01/04/07/08/09/10 | 重排/向量/BM25 性能、权限档位、多知识库、记忆管理、headless 入口 |
| P2（平台化，先设计后实现） | PERF-13/14、FREE-02/06/11/12/14/15 | 后台任务、子代理、多模态、跨平台、API 多客户端 |

## 工作纪律

1. 每个条目一个独立提交（`perf:` / `feat:`），先写基准或失败测试，再优化实现。
2. 所有新开关显式、默认关闭或保持现有默认值；改动写入 `.env.example` 与 `docs/`。
3. 不得放宽沙箱、确认、审计、路径边界；确需开放（如联网命令、额外根目录）必须做成显式选项并保留审计。
4. 不得修改用户数据：`.agent/`、`crud_tests/`、`data/raw/`、`logs/`、`.cache/`；基准与测试输出写入临时目录或 `.cache/reports/`。
5. 需要新依赖时优先可选 extras，并同步 `pyproject.toml` 与 `uv.lock`。
6. 性能条目必须在报告中留下「优化前/后」数字与测量方法；没有数据不计完成。

## 验收基线

```bash
uv sync --locked
uv run pytest -m 'not integration' -q                 # 必须全绿（当前 192 passed）
RUN_RAG_INTEGRATION=1 RAG_QUALITY_CORPUS=<parquet> uv run pytest -m integration -q   # 可选，本地已有模型时
uv run python dev/ci_sandbox_probe.py                 # Linux 隔离能力探测
```

性能测量统一入口建议新增 `dev/perf/benchmark.py`，输出 JSON 到 `.cache/reports/perf/`，覆盖：冷启动、会话加载、上下文准备、RAG 冷/热检索、重排、保存、导出、命令配额开销。
