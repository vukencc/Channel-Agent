# AI Agent Startup

Python 3.12+ 的流式 ReAct Agent 实验项目，包含工具调用、文件与命令沙箱，以及混合 RAG 检索。

## 启动

```bash
uv sync --locked
# 仅在 .env 不存在时，从 .env.example 创建并填写配置。
uv run main.py
```

## 目录

| 目录 | 职责 |
|---|---|
| `core/` | Agent 循环、模型调用、消息与日志 |
| `tools/` | 工具注册、文件/命令沙箱、Web/RAG 工具接口 |
| `rag/` | 文档分块、编码、向量/BM25 检索、融合与重排 |
| `data/raw/` | 应用知识库输入 |
| `tests/` | 当前工程回归与可选真实模型集成测试 |
| `benchmarks/rag/` | 独立工程验证入口和输入准备 |
| `benchmarks/rag/inputs/` | 受版本控制的固定输入 ID、查询及校验值 |
| `benchmarks/rag/archive/full_corpus/` | 暂停的全量评测、协议、基线及专属测试 |
| `scripts/rag/` | 资源下载和可选模型量化工具 |
| `docs/` | 使用文档与人工维护的历史交付记录 |
| `reports/` | 本地生成的评测输出，不提交 |
| `.cache/` | 本地模型、数据下载、向量缓存，不提交 |

## 验证

```bash
uv run pytest -m 'not integration'
# 已有本地模型时：
RUN_RAG_INTEGRATION=1 uv run pytest -m integration -k 'not strictness'
```

小规模公开数据验证见 [RAG 文档](docs/rag.md)。全量评测不属于默认测试或工程验证入口；不自动校准阈值。
