# AI Agent Startup

Python 3.12+ 的流式 ReAct Agent 实验项目，包含工具调用、文件与命令沙箱，以及混合 RAG 检索。

## 启动

```bash
uv sync --locked
# 仅在 .env 不存在时，从 .env.example 创建并填写配置。
uv run ai-agent-startup
```

```bash
uv run --extra web ai-agent-web
```

## 模板与分支开发

本仓库作为模板基线维护：`main` 保持可用，历史版本用注解 tag 冻结（当前 `v0.2.1`）。

```bash
# 从基线开启一个新方向
git switch -c feat/<topic> v0.2.1
# 并行开发（不来回切分支）
git worktree add ../ai-agent-<topic> v0.2.1 -b feat/<topic>
```

较大分歧方向可在 GitHub 上用「Use this template」新建独立仓库（只复制默认分支内容）。
贡献规范见 [贡献指南](CONTRIBUTING.md)，版本与标签流程见 [发布说明](docs/release.md)。

## Docker（开包即用）

```bash
if [ ! -f .env ]; then cp .env.example .env; fi        # 首次使用时创建并填写模型凭据
docker pull ghcr.io/vukencc/channel-agent:v0.2.1       # 或 docker load < ai-agent-startup-v0.2.1-image.tar.gz
docker run --rm -it --env-file .env -e TERM=xterm-256color \
  -v ai-agent-state:/data/state -v ai-agent-sandbox:/data/sandbox -v ai-agent-rag:/data/rag-cache \
  ghcr.io/vukencc/channel-agent:v0.2.1
```

容器已内置 CPU 版 torch 与混合 RAG 依赖（约 2.3GB）；首次 `rag_search` 会自动下载本地模型到
`/data/rag-cache`。命令沙箱、headless、compose、镜像归档与发布见 [Docker 指南](docs/docker.md)。
本地构建镜像见 [Docker 指南](docs/docker.md)。

## 结构

```text
src/ai_agent_startup/  应用包
  main.py              控制台入口（ai-agent-startup）
  config.py            环境配置与启动校验
  core/                Agent 循环、模型调用、会话与持久化
  tools/               工具注册、文件操作与命令沙箱
  rag/                 分块、编码、检索、融合与重排
tests/                 默认回归与真实模型集成测试
scripts/               跟踪的 CI 探测与 RAG 数据/质量工具
dev/                   本地开发资料、基准、归档与历史报告（不随 clone 分发）
.codex/、AGENTS.md      本地 Codex 配置与指令（不随 clone 分发）
docker/                Dockerfile 与 compose（.dockerignore 必须在仓库根）
docs/                  使用文档与历史交付记录
data/raw/              应用知识库
.cache/                本地模型、下载、索引及 reports/ 输出（不提交）
```

应用可作为包安装（`uv sync --locked` 后使用 `uv run ai-agent-startup` 启动）。
暂停的全量评测归档在 `dev/rag/archive/full_corpus/`；仅在保留该本地目录的工作区可用，不参与默认测试。

## 验证

```bash
uv run pytest -m 'not integration'          # 离线回归
uv run python scripts/ci_sandbox_probe.py   # Linux：真实 Bubblewrap/prlimit 探测
# 已有本地模型与公开语料时：
RUN_RAG_INTEGRATION=1 RAG_QUALITY_CORPUS=/path/to/corpus.parquet uv run pytest -m integration -q
uv run python -m scripts.rag.prepare_engineering
uv run python -m scripts.rag.run_engineering
```

模型准备和配置见 [RAG 文档](docs/rag.md)。小规模验证不自动校准阈值。
完整环境配置索引与本地 `.env` 同步方式见[配置说明](docs/configuration.md)。
结构化对话历史与上下文压缩为可选功能，默认关闭；配置和安全边界见[上下文管理说明](docs/context-management.md)。

## 依赖维护

`pyproject.toml` 只声明当前功能使用的依赖，`uv.lock` 由 `uv lock` 生成并提交，保留跨平台锁定和完整性校验。
移除了未使用的 LangChain、LlamaIndex、绘图和数据分析依赖；归档基线专用的 ChromaDB 仅在显式运行旧代码时安装，见归档说明。
更改依赖后运行 `uv lock` 和 `uv sync --locked`，不要手动截断锁文件。

命令执行需要 Linux 系统的 Bubblewrap；文件工具及主动执行规则见 [沙箱说明](docs/sandbox.md)。

## 多会话 CLI

`uv run ai-agent-startup` 现在启动全屏 Agent CLI。Ctrl+N 新建会话，左侧切换 Agent，Enter 发送，
Ctrl+C 停止当前任务，Ctrl+Q 退出。多个会话并发执行，各自拥有历史、记忆和工作区。
`/remember 内容` 保存记忆，`/export md` 或 `/export json` 导出实体文件；重启自动恢复 `.agent/` 下的数据。
每个根会话拥有独立项目工作区，子 Agent 自动共享直接父 Agent 的项目文件；上下文、记忆和审计仍独立。工具输出写入对应根会话的 `crud_tests/<会话ID>/`，`/where` 查看实际路径。PageUp/PageDown 浏览完整历史，Ctrl+End 跟随最新，F2 扩大对话区域。
完整操作与持久化说明见 [CLI 指南](docs/cli.md)，命令选择、权限模式和会话回收见 [CLI 命令与权限模式](docs/cli-command-modes.md)。
子 Agent 创建、父子持久消息和限制见[会话工具说明](docs/session-tools.md)；本轮实现、回归验证与合成 UI 基准见[CLI/session 工具交付报告](docs/cli-session-delivery-2026-09-29.md)。
可选本机浏览器界面安装与使用见 [WebUI 指南](docs/web-ui.md)。WebUI 默认无需令牌并只监听本机，支持预览并清理缓存、日志及会话记录；它与 CLI 共用会话状态目录，同一状态目录不能同时由两个进程打开。
可选任务计划默认关闭，启用后的工具与限制见 [任务计划使用说明](docs/task-plans.md)。

## 可选质量回归

冻结公开 qrels 的小候选池入口与报告见 [质量校准说明](docs/rag-quality.md)。
`uv run python -m scripts.rag.quality --corpus /path/to/corpus.parquet --output /tmp/new-quality-run`，
需显式配置本地 EMBEDDING_LOCAL_PATH/RERANK_LOCAL_PATH。输入和输出分开；修改阈值或模型须附校准与留出报告，不自动覆盖默认配置。

## 许可证

本项目使用 [MIT 许可证](LICENSE)。
