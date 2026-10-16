# AI Agent Startup

Python 3.12+ 的 Agent 项目，提供全屏 CLI、本机 WebUI、多 Agent 会话、混合 RAG、结构化上下文与可选监督器、运行调试。工具执行受权限确认、沙箱和审计约束；详细配置与开发入口见[文档索引](docs/README.md)。

新会话接手本地工作状态，请先读[会话交接说明](docs/session-handoff.md)。

## 安装与启动

```bash
uv sync --locked --extra web
if [ ! -f .env ]; then cp .env.example .env; fi
uv run --no-sync ai-agent-startup
```

`.env` 不存在时才从模板创建，再填写模型服务凭据。已有 `.env` 保持不变。`--extra web` 保留 WebUI 依赖；启动 WebUI：

```bash
uv run --no-sync ai-agent-web
```

WebUI 默认监听本机 `127.0.0.1:8765`。默认权限策略为 `smart`；无交互的 headless 任务默认为 `readonly`。权限模式、确认和沙箱边界见[配置说明](docs/configuration.md)与[沙箱说明](docs/sandbox.md)。

## 功能与文档

- 多会话 CLI、子 Agent 与父子消息：[CLI 指南](docs/cli.md)、[会话工具](docs/session-tools.md)
- 本机浏览器界面与只读运行调试：[WebUI 指南](docs/web-ui.md)、[DEBUG 运行调试](docs/debug-runtime.md)
- 混合检索、索引与质量验证：[RAG 指南](docs/rag.md)、[RAG 质量校准](docs/rag-quality.md)
- 结构化历史与可选监督器：[上下文管理](docs/context-management.md)
- 完整环境变量说明：[配置说明](docs/configuration.md)
- 新会话入口：[交接说明](docs/session-handoff.md)；全部指南、设计边界与历史记录：[文档索引](docs/README.md)

## 开发验证

```bash
uv run --no-sync pytest -m 'not integration' -q
uv run --no-sync python scripts/ci_sandbox_probe.py
```

## 项目布局

应用代码位于 `src/ai_agent_startup/`，测试位于 `tests/`，可直接运行的 CI/RAG 脚本位于 `scripts/`，Docker 资产位于 `docker/`，知识库原文位于 `data/raw/`。`docs/README.md` 说明文档的现行用途、设计边界和历史性质。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
