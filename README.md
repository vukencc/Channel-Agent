# AI Agent Startup

Python 3.12+ 的流式 ReAct Agent 实验项目，包含工具调用、文件与命令沙箱，以及混合 RAG 检索。

## 启动

```bash
uv sync --locked
# 仅在 .env 不存在时，从 .env.example 创建并填写配置。
uv run main.py
```

## 结构

```text
main.py / config.py  应用入口与配置
core/               Agent 循环、模型调用、消息与日志
tools/              工具注册与沙箱接口
rag/                分块、编码、检索、融合与重排
data/raw/           应用知识库
dev/                开发辅助功能的唯一入口
  tests/            默认回归与真实模型集成测试
  rag/              数据/模型准备、小规模验证、固定输入与归档
docs/               使用文档与历史交付记录
.cache/             本地模型、下载、索引及 reports/ 输出（不提交）
```

原 `benchmarks/`、`scripts/`、`tests/` 合并到 `dev/`，原 `reports/` 移至 `.cache/reports/`。
应用模块路径保持稳定。暂停的全量评测在 `dev/rag/archive/full_corpus/`，不参与默认测试。

## 验证

```bash
uv run pytest -m 'not integration'
# 已有本地模型时：
RUN_RAG_INTEGRATION=1 uv run pytest -m integration -k 'not strictness'
uv run python -m dev.rag.prepare_engineering
uv run python -m dev.rag.run_engineering
```

模型准备和配置见 [RAG 文档](docs/rag.md)。小规模验证不自动校准阈值。

## 依赖维护

`pyproject.toml` 只声明当前功能使用的依赖，`uv.lock` 由 `uv lock` 生成并提交，保留跨平台锁定和完整性校验。
移除了未使用的 LangChain、LlamaIndex、绘图和数据分析依赖；归档基线专用的 ChromaDB 仅在显式运行旧代码时安装，见归档说明。
更改依赖后运行 `uv lock` 和 `uv sync --locked`，不要手动截断锁文件。

命令执行需要 Linux 系统的 Bubblewrap；文件工具及主动执行规则见 [沙箱说明](docs/sandbox.md)。
