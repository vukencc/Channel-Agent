# 平台能力与扩展边界

> 文档类型：开发与设计参考 · [文档导航](README.md) · [三模块架构](architecture.md)

本文区分当前实现和后续设计。使用入口见 [快速上手](getting-started.md)，模块依赖见 [架构](architecture.md)，已做过的验证见 [阶段状态](project-status.md)。旧交付报告中的“仅设计”以其记录日期为准。

## 当前入口与平台

| 环境 | 会话、界面与文件工具 | 命令执行 | 支持边界 |
| --- | --- | --- | --- |
| Linux | CLI/headless、WebUI、原生 Qt GUI | sandbox 使用 Bubblewrap/prlimit；可显式选择 local | 当前开发和验证环境，仍需实际隔离探针及 GUI 系统库 |
| WSL2 Linux | 同 Linux；Qt 需 WSLg，建议项目置于 Linux 文件系统 | 同 Linux，能力不足则拒绝 | Windows 终端通过 WSL 调用 Linux uv/Python/Node；不等于 Windows 原生后端 |
| macOS 原生 | 有基础平台适配，完整依赖、交互和文件边界未验收 | 当前命令入口拒绝，建议 Linux 环境 | 不作为已验收部署目标 |
| Windows 原生 | 原生 Qt、Web 和 Core；项目目录使用本地盘路径，文件访问拒绝重解析点等不安全路径 | sandbox 通过 WSL2 中的 Linux 执行器；local 拒绝 | 后台命令任务与维护清理不可用；本轮验证范围见 [Windows 指南](windows.md) |

`/platform` 或 `uv run --no-sync ai-agent-startup --platform` 只报告静态能力，不创建会话或调用模型。找到可执行文件不等于命名空间、资源限制和网络实际可用；用 `python -m agent_test.scripts.ci_sandbox_probe` 验证。状态目录由单个进程独占；多个界面进程须分别使用 `--state-dir`，不能同时写同一目录。

Linux 经典会话 local 使用宿主工具链、网络和真实路径；Flow local 为防止越出所选项目，另通过 Bubblewrap 限制持久写入范围。Windows 不支持 local 命令。不能把两者安全语义混用；完整规则见 [沙箱](sandbox.md)。网络文件系统的锁与目录身份行为还需部署环境验证。

## Headless 单次任务

已提供 `--prompt`、`--json`、`--state-dir`、`--session`、`--policy`，不会启动 TUI 或读取交互键盘。默认 `readonly`，需要人工确认时拒绝；显式 `full_access` 可自动批准工具确认，但路径、能力上限和环境边界仍有效。

```bash
uv run --no-sync ai-agent-startup --prompt "说明项目结构" --json --policy readonly
```

`--json` 输出一个版本化对象，含会话、轮次、状态、消息、指标和错误；诊断走 stderr。退出码 0 表示正常结束，2 为参数/配置错误，3 为检查点未完成，4 为执行失败，130 为取消。完整选项以 `--help` 和 [CLI 指南](cli.md) 为准。尚未提供 `--jsonl` 和独立远程审批通道。

## 已实现的本机 HTTP API 与 SSE

WebUI 已提供本机 FastAPI 服务，默认 `127.0.0.1:8765`；不是尚未监听的设计稿。原生 GUI 直接组合 Core，不经 HTTP。当前服务主要端点如下，实际参数模型和状态码以 `src/agent_frontend/web/app.py` 为准：

| 用途 | 当前端点 |
| --- | --- |
| 状态、会话与消息 | `GET /api/sessions`、`POST /api/sessions`、`POST /api/sessions/{id}/messages` |
| 状态流、停止与确认 | `GET /api/events`、`POST /api/sessions/{id}/stop`、`POST /api/sessions/{id}/confirmation` |
| 项目与目录选择 | `/api/projects`、`GET /api/workspaces/directories` |
| Flow 定义与运行 | `/api/flows`、`POST /api/flows/{id}/run`、`POST /api/flows/{id}/cancel` |
| 创作与便携模板 | `/api/flow-designer/chat`、`/api/flow-designer/cancel`、`/api/flows/import`、`/api/flows/export` |
| 模型与只读计划 | `/api/models`、`GET /api/sessions/{id}/plan` |
| Debug（显式开启） | `/debug`、`/api/debug` 及 AgentTest 诊断路由 |

服务包含本机 Host/来源检查和请求大小边界。SSE 推送状态概要，详情按需读取；重连不重放工具副作用。现有 API 是本机单用户界面的组成部分，不是已经验证的公网多租户服务；默认地址和部署边界见 [WebUI](web-ui.md)。未来若增加远程审批，需要绑定 session/turn/tool_call、原文摘要、有效期和单次使用，不能仅增加默认批准参数。

## 取消与进程生命周期

运行时在检索、嵌入与重排批次之间协作取消，工具有独立超时；写操作须等待真实执行结束，避免停止后迟到写入。命令取消回收进程组并等待收尾，Flow 还要确认子任务、工具和后台命令排空，才释放项目并决定下游状态。

原生模型加载、单次推理批次和在途网络操作不能都由 Python 安全强杀，不承诺所有路径瞬时停止。常驻独立推理 worker、进程级 deadline 和可重建的只读推理队列仍是后续设计，需先验证超时回收、请求不串轮和内存预算；不能把工具取消实现当成该架构已完成。

## 尚未实现的扩展

- **MCP 客户端与动态工具市场**：当前工具仍由 Pydantic 参数和 `register_tool` 注册。未来接入须声明来源、只读/写/出网能力、确认、时限及 schema 预算，不可信服务不能自行免确认。
- **通用浏览器与音视频工作流**：已有可选图片附件与模型请求适配，但协议测试不等于所有视觉服务通过；暂无已验收的通用浏览器、音频或视频工具。网页工具仍须遵守出网审批、地址和大小边界。
- **Docker/Podman 命令后端**：仓库提供应用镜像和 Compose，并不等于用容器替代 Bubblewrap 的工具执行后端。后者需固定镜像、非特权、工作区唯一可写、无宿主 socket/凭据、资源限额与完整取消/越界测试，目前未实现。
- **Flow 并行及条件调度**：当前拓扑串行；分支节点通过指令处理输入数据，没有框架级条件表达式或并行执行。提示词职责也不是逐节点文件访问隔离。

## CI 与验证

`.github/workflows/tests.yml` 在 PR、main 及列出的开发分支 push 上运行锁定依赖、真实隔离探针和离线回归。使用 Ubuntu 22.04、Python 3.12，安装 Web/desktop extras、Bubblewrap/util-linux 和 Qt 系统库；隔离探测失败须非零退出。

默认 PR 不下载 RAG 模型或调用付费 API。`workflow_dispatch` 可显式启用公开模型/语料的 integration 任务，准备配置见 [RAG 指南](rag.md)。具体 PR 是否通过以 GitHub 当前检查为准，配置文件或历史 CI 成功都不是新提交的通过证明。发布工作流另验证注解 tag、版本和 main 祖先关系，详见 [发布说明](release.md)。
