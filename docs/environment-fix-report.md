> 历史报告，不作为当前使用规范；保留当时的问题描述和实测结果。当前说明见[当前文档索引](README.md)。

# 环境检测与依赖准备修复报告

日期：2026-10-15。前两项已实现；DEBUG 本次只交付设计，不代表新版时间线 UI 已上线。

## 问题一：遇到环境缺失就退回静态检查

原系统有通用命令工具，但提示词主要强调隔离与断网，没有“探测 → 安装依赖 → 构建 → 测试”的完整流程，也没有根据项目清单准备环境的工具。找不到命令、拒绝确认和资源不足没有清楚区分，模型容易直接认定不能运行。

本轮增加 `tools/environment.py`：`inspect_environment` 在真实沙箱探测六类运行时；`prepare_project_environment` 读取清单，生成包含 SHA256、目标、脚本策略、网络与配额的权限计划，获准后实际安装 npm/uv 依赖。默认 npm 禁用生命周期脚本；uv 只安装 wheel，使用独立 `.agent-env/python`，保留用户 `.venv`。安装成功仍要求继续真实构建和测试。

新建及恢复会话共同注入简短环境工作流；静态检查或安装成功不能等同于项目验证完成。命令未找到、地址空间不足及本地网关拒绝都有具体诊断。

真实实验还发现，uv 默认并发安装在本机 512 MiB 地址空间中分配失败。准备工具将下载、安装及构建并发限制为 1；同一公开 wheel 随后安装成功，没有增加普通命令内存限额。设置依据见 [uv 官方环境变量说明](https://docs.astral.sh/uv/reference/environment/)。

新工具增加固定上下文开销，暴露旧长文件测试过早停在检查点的回归。现在仅在仍超预算时回收较近、已完成调用的大参数，保留最新完整批次、待执行参数、真实回执及持久原文；未提高上下文预算或弱化旧测试。

能力范围：直接支持 npm 与 uv 项目依赖准备，不会自动在宿主安装 JDK、Maven、系统包或任意运行时。源码构建依赖可能被默认 wheel-only 规则拒绝。这些情况必须说明具体阻碍，不能宣称完整测试通过；提示词与工具改善模型选择，并不保证每次采用完全相同的调用顺序。

## 问题二：已安装 Node 仍被报告不存在

Windows 有 `D:\js\run\node.exe`，WSL 也有 `/home/elaine_vuken/.nvm/versions/node/v24.21.0/bin/node`。原沙箱清空宿主环境，仅提供 `/usr/bin:/bin`，看不到 NVM，也不应直接执行 Windows PE 程序。

找到二进制也不代表能运行 V8。真实 bubblewrap + prlimit 实验：

| Linux Node v24.21.0 地址空间上限 | 实际脚本执行结果 |
| --- | --- |
| 512 MiB，普通命令默认值 | 退出 133，V8 内存初始化失败 |
| 1024 MiB | 退出 133，CodeRange 虚拟内存预留失败 |
| 2048 / 4096 / 8192 MiB | 成功输出算术结果 |

新增 `tools/runtime_paths.py`，根据可信配置精确只读挂载 Linux Node、npm 子树及 uv 文件。校验 ELF、架构、链接和保护目录重叠，不挂整个 home、Windows 目录或宿主虚拟环境。普通命令及后台任务均支持 `runtime_profile="node"`，审批展示其有界地址空间额度；system 仍为 512 MiB，CPU、文件、进程数及网络限制保留。

2 GiB 是虚拟地址空间限额，不是常驻物理内存测量；其他版本和大型项目仍需独立评估。

## 新配置与本机设置

| 配置 | 默认 | 本机当前设置 |
| --- | --- | --- |
| `COMMAND_RUNTIME_PATHS` | `{}` | 现有 Linux NVM Node 前缀及 Linux uv 文件 |
| `COMMAND_NODE_MEMORY_MB` | `0`，关闭 Node 档位 | `2048` |
| `ENABLE_ENVIRONMENT_PREPARE` | `false` | `true` |
| `ENVIRONMENT_PREPARE_TIMEOUT` | `120` 秒，允许 1–600 | `120` |

忽略的 `.env` 已同步，保留凭据，并显式开启现有白名单网络配置，仅允许 `registry.npmjs.org`、`pypi.org`、`files.pythonhosted.org`。模板默认仍关闭安装、Node 档位和联网。Smart/standard 按计划确认；Full Access 沿用自动批准语义，路径、沙箱、配额、取消与审计继续有效。`.env` 不进入 Git。

没有新增 Python 依赖、extras 或模型，`pyproject.toml` 和 `uv.lock` 无需变更。使用说明见[环境准备指南](environment-preparation.md)。

## 验证结果与限制

```sh
uv sync --locked
uv sync --locked --extra web
UV_NO_SYNC=1 uv run pytest -m 'not integration' -q
UV_NO_SYNC=1 uv run python dev/ci_sandbox_probe.py
```

均使用 WSL uv；测试进程 PATH 指向现有 Linux NVM Node。完整非集成回归 **1064 passed、2 skipped、5 deselected，33.13 秒**。两项跳过是未安装可选 ANN 的回归，五项 integration 未运行。真实 bubblewrap + prlimit 隔离检查通过。

`scripts/environment_validation.py` 使用临时工作区及公开依赖 `is-number@7.0.0`、`idna==3.10`。tgz 完整性与官方 npm 元数据校验，wheel SHA256 与 PyPI 元数据校验；沙箱内完成 npm 安装、真实构建、两个 Node 功能测试、uv 安装及两个 Python 功能测试。

当前 DNS 将包仓库解析为 `198.18.*` FakeIP，被原非公网拒绝规则拦截。直接联网安装实测失败，未放宽规则，也未记为通过。独立离线实验使用预先下载并校验的公开文件，生产工具没有宿主下载或代理绕过回退。

- [联网受阻原始结果](../.cache/reports/environment-validation-1792038689962762941.json)
- [离线安装、构建和功能测试通过的原始结果](../.cache/reports/environment-validation-1792039144427709711.json)

新增回归覆盖安装关闭、拒绝/超时确认、越界、前后置符号链接替换、恶意同名 Python 模块、`.npmrc` 全局安装重定向、Node 档位及后台透传、FakeIP 诊断、上下文预算和持久原文不变。安装并非事务操作；失败时可能保留项目内部分依赖，工具不自动删除用户文件。

## DEBUG 具体计划

详见[调试时间线设计计划](debug-timeline-design.md)，按五阶段推进：

1. 因果事件、递增序号、游标和丢失窗口协议。
2. 真实的模型、工具、任务、阶段、父子 Agent 及通信埋点。
3. 鉴权只读详情、上下文采样和 token 来源标记。
4. 向右延伸的上下事件轨道、工具组展开、子 Agent 分叉与通信连线。
5. 并发、断线重连、安全、可访问性和性能验收。

上侧放最终回复、停止及任务/阶段切换，下侧折叠连续工具批次；展开详情置下。子 Agent 使用独立颜色，共用时间轴；通信为有向连线。上下文百分比与每请求/累计 input/output tokens 使用独立刻度，区分 provider、estimated、未知值，标注 Compact 前后变化。暂停画面不暂停 Agent；普通事件不记录 prompt、reasoning、密钥或完整命令，详情按需鉴权并脱敏。
