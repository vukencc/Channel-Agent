# 新会话交接说明

> 更新日期：2026-10-16。当前交付分支为 `codex/live-project-validation`，已基于 `origin/main` 的 `5d1d8c8` 快进同步。本轮功能变更已提交为 `c0cd0dd`；远端推送因 GitHub HTTPS 连接超时尚未完成，PR 尚未创建。恢复连接后先推送此分支，再创建目标为 `main` 的 PR 并更新本交接记录。

新会话开始时先运行 `git status --short`，保留所有已有状态。不要执行 reset、clean、rebase，也不要切换到旧 tag，以免丢失当前长期实现。`.agent/`、`crud_tests/`、`data/raw/`、`logs/` 和 `.cache/` 含用户状态或生成数据，应保留；需要新报告时只在 `.cache/reports/` 下新建。不要打印 `.env` 内容或密钥，也不要自动重跑真实模型任务或清理缓存。

## 建议阅读顺序

1. 根目录 `AGENTS.md`：本地工作和代理调度约定。
2. 本文：当前工作状态、环境和最近验证事实。
3. [文档索引](README.md)：现行指南、设计边界与历史记录分类。
4. 按工作内容阅读对应指南。DEBUG 以[运行调试说明](debug-runtime.md)和[清晰度报告](debug-overview-clarity-report.md)为准；旧交付截图不能代表当前布局。

## 当前实现与验证边界

DEBUG 总览根 Agent 行高 224 px，子 Agent 默认为 72 px，点击后展开为 224 px。共享 fork/join 母线表达父子结构，不交叉；通信在局部详情中逐条呈现。异常按 session、turn、tool_call_id 去重，保留原始记录与上下文/token 信息。

既有真实模型实验 2 完成：1 个父 Agent 与 2 个子 Agent，740.174 秒、125 次模型请求、158 次工具调用、6 条发送和 9 条读取消息、0 次 Compact，峰值上下文利用率 64.49%。本地 UI 当前回放的是既有 4460 个事件、3 个 Agent 的 trace；它不是新模型调用。新视觉仍需用户手工复验。

2026-10-16 最近一次根目录非集成回归记录为 1235 passed、2 skipped、5 deselected，用时 40.52 秒。这是当时的结果，不是后续提交的永久保证。CPU/时间开销高的真实模型评估按需执行，不属于默认检查。

本次文档交接检查覆盖 45 份 Markdown、206 个本地链接和 8 份 TOML；链接、锚点、索引和配置解析通过。WSL Codex 0.160.0 的 `codex --strict-config doctor --json` 返回 `config.load: ok`，指令预览可读取新版 AGENTS 与交接路径；未以真实推理验证每个自定义角色。详细验证清单在 `.cache/reports/docs-handoff-20261016-02782df1/validation.json`。本次没有新跑真实模型任务，也没有启动常驻 WebUI 服务。

## 运行环境

项目实际运行环境为 Linux/WSL：仓库 `/home/elaine_vuken/projects/ai-agent-startup`，`uv` 位于 `/home/elaine_vuken/.local/bin/uv`，Node 来自 `/home/elaine_vuken/.nvm/versions/node/v24.21.0/bin/node`，Python 使用项目 `.venv/bin/python3.12`。系统 Python 3.14 与项目虚拟环境不同。非登录 shell 中使用 Node 前需把上述 nvm `bin` 目录加入 `PATH`。

Windows 桌面当前工作目录可能显示为 `\\wsl$\Ubuntu\home\elaine_vuken\projects\ai-agent-startup`；从该 UNC 路径运行 Windows `uv` 可能重建 `.venv`。应在 WSL Linux 中运行 `uv`、Python 和 Node。通常安装命令为 `uv sync --locked --extra web`；依赖没有变化时，可用 `uv run --no-sync ...` 避免同步环境时移除现有可选依赖。此前记录中的 `UV_NO_SYNC=1 uv run ...` 是当时使用的命令形式。

回归应在 Linux/WSL Node 环境运行，避免 FakeDOM 缺失导致相关测试误跳过。`scripts/ci_sandbox_probe.py` 会实际检查 Linux 沙箱能力；不支持时如实报告。最近报告和截图在 `.cache/reports/debug-clarity-20261016-b84e14/`，此前 `debug-flow` 截图已被新布局替换，不要删除旧产物。

## 版本控制边界

本地 `AGENTS.md`、`.codex/` 和 `dev/` 被 Git 忽略，当前 checkout 内仍可供新会话读取，但 fresh clone 不会包含它们。推送与 PR 状态以远端分支和 GitHub 页面为准；不要把本地提交描述为已同步，除非远端已确认。新会话可先用一句话提示：“先阅读根 AGENTS.md 与 docs/session-handoff.md，检查 git status 并保留全部现有改动，再按 docs/README.md 找对应指南。”
