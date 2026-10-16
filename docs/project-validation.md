> 历史报告，不作为当前使用规范；保留当时的问题描述和实测结果。当前说明见[当前调试与上下文指南](debug-runtime.md)。

# 大型项目执行与上下文监督实测报告

## 结论与实验边界

2026-10-07 使用真实主模型、真实独立监督器及实际 WebUI，完成四轮 WorkBoard 项目观察和两次监督边界实验。Agent 会主动创建计划；可靠推进计划尚未确认。监督触发、网关兼容和 WebUI 状态问题已修复，但生成项目未通过外部验收，不能把仓库回归通过等同于 Agent 可可靠完成大型项目。

固定需求要求在空工作区实现 Python 标准库 WorkBoard：SQLite、业务校验、CLI、HTTP、导入导出、依赖环、原子事务、持久化、README，以及至少 30 项实际运行的自有 unittest。四轮沿用相同需求；工程轮恢复只附加“继续，保留已有文件”，未降低要求。主模型与监督器均使用本机配置的 deepseek-v4.1-flash，分别通过独立客户端真实调用。

实验入口 scripts/project_validation.py；外部验收 scripts/workboard_acceptance.py 包含预先固定的 32 项 API/CLI/事务/依赖/持久化/导入导出用例，源码不提供给 Agent。HTTP 属于项目需求，未另做外部 HTTP 验收。入口导入失败时测试无法展开，不计作 32 项已运行。项目自有测试单独执行；任一失败、拦截、取消、超时或无工作区均返回非零。

所有产物保留在系统 /tmp/agent-project-validation-*，独立保存工作区、状态、知识目录、审计、缓存、观察及验收文本。维护 API 也绑定实验目录，未清理真实用户数据。解析路径后检查临时目录边界；恢复要求固定需求匹配且状态/工作区不是链接。观察文件原子替换，工具命令网络关闭，实际使用既有 Bubblewrap + prlimit 隔离。模型 API 访问已配置服务。本轮未更改实际 .env/.env.supervisor，未增加依赖。

## 计划与分步执行

基线工具顺序为 list_files → run_command → plan_create → plan_update → create_file：先检查环境，再在写文件前创建并启动计划。模型并非只给一次性回答，但之后反复读取已有文件，没有更新完成步骤；恢复轮也没有恢复中断的 scaffold 步骤。

根因包括普通工具改变计划 revision，而提示缺少明确更新顺序、最新版本和真实证据；压缩后的状态也缺少最近操作定位。core/task_plans.py 现为运行时有界 JSON 添加最近四条真实配对证据（ID、工具名、来源、摘要）。sessions.py 和工具参数描述明确要求 plan_get 核对最新 revision/证据，再更新 completed 并执行下一步。core/execution_guidance.py 基于本轮 executed、配对结果、相同参数及结果哈希识别三次重复读取，提示推进或精确读取；不跨用户轮次生效，不拒绝合法工具，不自动完成步骤或伪造证据。

这些优化仍属行为引导。真实恢复轮仍停滞，不能声称解决可靠逐步执行。强制阶段调度或自动恢复中断计划需要独立设计；本轮未放宽证据、权限、版本校验。

## 监督器核心问题与修复

| 根因 | 最小修复 |
| --- | --- |
| 多个逻辑组被合并成单一存储 chunk，按 chunk 数跳过监督 | 对最近最多 24 个完整逻辑组评分，与存储分块解耦。 |
| 高频压缩只剩软保护组，被判历史不足 | 软保护组参与评分，当前真实用户独立保留。 |
| 只有软保护组，高方差判定后没有可压缩候选 | 有有效评分且达到阈值时晋升较旧一组，保留最新组；零阈值不能把未评分当高方差。 |
| 文本前缀采样遗漏重要尾部 | 独立配额内采样头/中/尾，含工具名和参数；实际 SDK 测试覆盖最小 1024 字符请求。 |
| 网关拒绝独立客户端的无会话头请求 | 独立稳定随机 x-opencode-session，不继承主会话身份。 |
| 服务拒绝空 tools 字段，返回截断或围栏 JSON | 不发送 tools/tool_choice；拒绝响应工具调用、显式截断、旁白、多文档；接受单一完整 JSON 围栏。缺省 finish_reason 仍为兼容模式。 |
| 主模型 thinking 设置无法控制独立监督器 | 新增独立 SUPERVISOR_THINKING_MODE，默认 auto 不发送扩展参数。 |
| 最新完成工具组始终受保护，巨量读文件触发硬预算 checkpoint | 必要时整组归档最新已完成往返；当前用户与未配对/不匹配组不归档。 |
| 摘录 fallback 丢失工具名和路径 | 保留有界调用定位、结果和失败来源，标记 extractive_fallback；完整原文继续保留。 |

评分请求有独立时限；每批压缩的块摘要与 Session 摘要共享截止时间，默认 20 秒。失败不会伪装成功，预算压缩走显式摘录。完整消息配对、原子写、路径边界和审计继续保留。

## 大型项目真实结果

表中工具尝试来自实际 assistant tool_calls，取消中的调用不等于执行成功。计划均为真实记录，所有轮次完成步骤为 0。源码数量只按实际 .py 文件计。

| 观察轮 | 源码文件 | 工具尝试 | 归档块 | 计划 | 结果 |
| --- | ---: | ---: | ---: | --- | --- |
| baseline | 5 | 35，执行 35 | 18 | 0/8 | 硬预算 checkpoint；36 次摘要 BadRequestError，MissingSessionID；外部导入失败。 |
| after，中间版本 | 4 | 22，执行 21 | 13 | 0/9 | 人工停止以修复新发现边界；有摘要成功，也有 JSON/超时错误。 |
| final，16k 预算 | 4 | 24，执行 23 | 13 | 0/8 | 监督检查 5 次成功；人工停止；外部导入失败。 |
| engineering，含恢复轮 | 4 | 75，前轮执行 45、恢复轮执行 30 | 40 | 0/8 | 人工停止重复读取；缺少包导出入口，无法 import Board；没有自有 tests 目录。 |

baseline/after 使用主输入 16000 token、监督 auto/20 秒；final 使用 16000 token、监督 disabled/20 秒；engineering 使用 32000 token、监督 disabled/60 秒。字符预算均 64000，工具轮次上限 48，命令网络 off，未安装项目依赖。各轮代码、配置及停止时间不同，不作受控提速结论。

工程恢复轮 16 次主模型请求合计 55.456 秒，监督评分/块摘要/Session 摘要合计约 447.45 秒（12.88/171.56/263.01 秒）。同步多页压缩和重复读取仍为成本瓶颈。监督器虽然已能运行，摘要仍有无效响应和降级：恢复轮评分 5 次成功；块摘要 31 次成功、3 次 JSONDecodeError、1 次 ValueError、1 次停止取消；Session 摘要 24 次成功、8 次 ValueError。

## 历史监督触发边界：真实模型实验（高占用门槛实施前）

scripts/context_validation.py 的历史是明确标识的合成测试输入，不冒充工具执行。输入预算足够以隔离方差判定，监督调用为真实 API。最新结果位于 /tmp/agent-project-validation-context-c2rqzc1u/results.json：

| 场景 | 真实评分 | 相对方差 | 归档 | 总耗时 |
| --- | --- | ---: | ---: | ---: |
| 8 组相关需求 | 全部 0.2 | 0 | 0 | 2.958 秒 |
| 8 组相关/无关交替 | 1/0 交替 | 1 | 1 | 7.351 秒 |
| 仅两组软保护历史 | 0.95/0.02 | 0.919226 | 1，最新组保留 | 7.849 秒 |

这是实施高占用门槛前的历史结果。后两例利用率约 6.5%，当时均因 value_variance 触发，而非硬预算；当前实现低于阈值不检查或压缩，因此不能用此表描述当前行为。新版 `scripts/context_validation.py` 分别校准低占用与 92% 高占用输入，并明确使用合成填充，当前结果见工作区优化报告。较早一次同样实验得到相关历史方差 0.024221、混合历史方差 1，说明评分存在波动。这些结果验证边界，不代表通用摘要质量评测。

## WebUI

修复中文动态 phase 统一显示“运行中”；顶部添加只读预算、归档数、监督状态、摘要降级、压缩原因和计划步数。计划按钮打开只读看板，区分 Agent 自报完成与外部验收。SSE/详情合并保留新状态，避免晚到请求覆盖；新轮次清除旧 recovery_context。配置错误只显示配置名及 Unicode 控制字符编号，隐藏 URL/凭据/服务响应正文。

实际页面检查了等待模型、整理上下文、取消、0/8 中断计划看板、执行记录折叠及历史加载入口。1280×900 桌面与 400×900 窄屏均检查；窄屏 body/document 宽度均为 400，无整页横向溢出；本次未记录浏览器 console error。

截图与日志位于 /tmp/agent-project-validation-engineering-20261007/：webui-desktop.jpg、webui-plan.jpg、webui-mobile.jpg、observation.json、observation-before-resume.json、acceptance.txt。四个实验服务均已停止，文件保留；系统临时目录不保证永久保存。

## 配置与验证

唯一新增配置：.env.supervisor.example 中 SUPERVISOR_THINKING_MODE=auto，支持 auto/enabled/disabled，仅影响监督服务，不增加工具权限或确认豁免，压缩继续审计。真实实验用进程变量显式 disabled/60 秒，实际配置未修改。没有新依赖，Web 验证安装现有 extra：uv sync --locked --extra web。

新增测试：tests/test_context_boundaries.py、tests/test_project_validation.py、tests/test_web_context_status.py；加强 tests/test_context_supervisor.py。关键边界执行 RED → 最小修复 → GREEN，未删除或弱化已有测试。单元测试 stub 仅用于确定性回归，上表来自真实请求。

全套离线回归以进程变量关闭可选功能，避免本机 ignored 配置污染旧默认行为 fixture；专门测试显式启用上下文和计划。一次直接继承本机启用配置的全套出现 fixture 预期不匹配并被终止，该次不计通过。最终命令及结果在下方记录。

```bash
uv sync --locked
env ENABLE_STRUCTURED_CONTEXT=false ENABLE_TASK_PLANS=false uv run --extra web pytest -m 'not integration' -q
env ENABLE_STRUCTURED_CONTEXT=true ENABLE_TASK_PLANS=false uv run --extra web pytest tests/test_context_boundaries.py tests/test_history_context.py tests/test_context_supervisor.py tests/test_history_tools.py tests/test_history_search.py tests/test_structured_sessions.py tests/test_project_validation.py tests/test_web_context_status.py -q
uv run python dev/ci_sandbox_probe.py
git diff --check
```

复现项目实验使用全新临时目录：

```bash
uv run --extra web python scripts/project_validation.py --root /tmp/agent-project-validation-new --port 8977 --max-seconds 900
uv run python scripts/workboard_acceptance.py /tmp/agent-project-validation-new
SUPERVISOR_THINKING_MODE=disabled SUPERVISOR_TIMEOUT=60 uv run python scripts/context_validation.py
```

首条命令只启动隔离 Web 服务；从页面新建会话，提交该脚本保存的 task.txt。--resume 仅恢复本脚本已停止的有效临时实验。未开展本地 RAG integration 评测，也没有为生成项目失败设通过例外。

## 最终回归记录

| 检查 | 实际结果 |
| --- | --- |
| uv sync --locked | 成功；依赖与锁文件无改动。 |
| 默认关闭可选功能的完整非 integration 回归（含现有 web extra） | 772 passed、2 skipped、5 deselected，22.19 秒；跳过为未安装可选 ANN，排除为 integration。 |
| 结构化上下文显式开启的八个针对性测试文件 | 81 passed，2.03 秒；包含真实配对工具执行的计划证据回归。 |
| dev/ci_sandbox_probe.py | 实际 Bubblewrap + prlimit 隔离执行通过。 |
| git diff --check | 通过。 |
| 只读代码复核 | 高方差零阈值防误触问题修复后，无剩余发现。 |
| 工程生成项目验收脚本 | 退出 1；导入失败、自有测试目录缺失。仓库回归不会覆盖该失败结论。 |

本轮在 codex/live-project-validation 分支保留源码、测试、模板与文档改动，未提交或推送。
