# 五 Agent 实际业务测试：团队共享设备借用台（2026-10-31）

> 文档类型：历史报告 · [当前阶段状态](project-status.md) · [文档导航](README.md)。保留当时的结果与失败；旧路径、默认值及本地产物不作为当前使用要求，也不保证随 clone 提供。

## 场景与目标

20 人团队共用投影仪、相机和测试设备。管理员录入设备，成员借用和归还，系统维护库存；超量借用、重复归还和并发争借都不能破坏库存。交付可运行的标准库 HTTP 后端、SQLite 数据库、中文原生前端、实际测试与使用说明。

本用例验证多个 Flow 节点在各自阶段工作、共用真实项目、交接与错误传播，并独立验证最终业务。不把“节点 completed”或模型自报等同于业务通过。项目起始为空，运行状态、日志与凭据和用户现有数据隔离。全部既有代码改动保留。

## 流程与责任

| 节点 | 输入 | 工作和交付 | 给下游的交接 |
| --- | --- | --- | --- |
| 01 需求与接口设计 | 用户业务契约和空目录 | SPEC.md：接口、表、事务、页面与验收 | 约束、接口、实现注意事项 |
| 02 后端与库存事务 | 实际交接、SPEC.md | app.py：SQLite、HTTP、输入校验、并发借还 | 已实现接口、编译检查和未测事项 |
| 03 界面与业务交互 | 后端交接、SPEC.md | 三个静态文件，设备/借用/归还/筛选/统计 | 文件、API适配和待测交互 |
| 04 测试与缺陷修复 | 前端交接与实际文件 | test_app.py、QA.md；至少15项真实HTTP测试并修复 | 实际命令、缺陷和结果 |
| 05 交付与运行核对 | QA交接与报告 | 重跑测试、README.md、DELIVERY.md | 启动、演示、结果和剩余限制 |

五个节点串行连接，各有独立会话，共用一个由 Project 授权的目录。节点指令要求先读交接、只执行本阶段，不再委派替代下游。精确契约与提示词位于 `src/agent_test/scripts/cases/equipment_lending_flow.json`；导入辅助模块为 `agent_test.scripts.equipment_flow_case`，创建模板与项目时不调用模型，也不改现有项目文件。

## 固定验收契约

- 仅 Python 标准库、SQLite 和原生 HTML/CSS/JS，无下载依赖。启动 `python app.py --host 127.0.0.1 --port PORT --db-file ABS_SQLITE`。
- 设备含 id/name/total/available；借用含 id/equipment_id/borrower/quantity/status。完整 API 和错误格式见用例 JSON。
- 名称/借用人 trim 后 1–80 字符；库存/数量为严格整数 1–10000，拒绝 bool。错误返回 JSON 400/404/409，失败前后库存、记录和统计保持一致。
- 5 件设备同时被两个请求各借 3 件，只能一个成功；剩余 2 件、一个活动借用。归还恢复 5，再归还返回 409，库存仍 5。
- 独立数据库，实际重启后保持借用与归还；SQLite 参数化查询与事务。页面真实接入 API，显示错误，拒绝静态路径穿越，用户文本安全显示，450 px 可用。

## 计划与执行步骤

1. 阅读仓库规范、交接和当前指南，检查 Git 状态/分支/最近提交，保存测试前差异；不覆盖旧改动。
2. locked sync web/desktop 依赖。在新的系统临时目录建立空项目、独立状态、审计、默认工作目录、知识与缓存根。
3. 加载五节点 JSON，创建无目录模板，再由新 Project 导入独立副本；项目显式授权该空目录。打开独立 Web 服务，在真实浏览器选择项目并点击运行。
4. 每次模型调用透传真实 provider，记录脱敏 outcome/token/请求次数和节点状态时间线；不替换回复或工具结果。核对各节点状态、不同 session、相同工作区、父输出完整传入下游及工具/结果配对。
5. 节点完成后，由独立验收器启动实际 app.py，使用自己的 SQLite 文件和 HTTP 请求，验证业务、边界、并发和重启。失败则记录固定检查名称与错误类型，交给 Flow 修复，不由父代理代写业务。
6. 独立启动同一产物，用真实浏览器完成录入、借用、错误、归还、筛选和统计；检查手机宽度与脚本异常。
7. 完成聚焦和非集成回归，复核测试工具，导出脱敏证据，停止自有服务并精确清理私有状态，保留测试报告与明确标注的生成产物。

## 本轮执行配置

使用当前已有 provider/model，模型名称 `deepseek-v4.1-flash`。保留默认完整工具目录、内部委派和计划功能的配置；节点职责通过提示词约束。独立验收进程设置输出 max_tokens=8192、总模型调用时限 240 秒、网络空闲时限 90 秒。最终复验对本次 local 命令另设 120 秒上限；前三轮沿用原 600 秒默认。模型连接错误/超时仅在尚未产生可执行回复时做至多四次有界请求；每次尝试单独记录，生产策略未修改。整个用例 180 次模型回调、运行 1800 秒的保护只属于该测试驱动。

Project 选择 Linux local 和 full_access，Flow 原有写入保护、目录身份校验、权限审计与预约保持。关闭本测试不相关的 RAG 评估/记忆自动提取，将所有可写基础设施路径指向新的临时目录。用户 .env、.agent、crud_tests、旧缓存和运行中的服务不动。

## 问题与结果

### 当前问题记录

- **NET-01，首轮**：需求节点尚未调用工具，四次模型回调均为 APIConnectionError / SSLEOFError / connect（每次内含两次连接尝试）。Flow failed，下游 blocked，项目仍为空。保留 `progress-attempt-01.json` 与运行状态。
- **NET-02，第二轮**：原进程原配置重试后成功执行需求阶段工具并生成 SPEC.md；随后连接再次失败，Flow 在需求节点阻断。保留原结果，不把规格文件当作应用完成。
- **NET-03，对照**：同模型/凭据/内容、83,677 bytes 的真实流式 HTTP 请求，经环境代理 ConnectError；trust_env=False 得到 HTTP 200 和首块。短流式请求带完整工具目录也曾成功。对照只用于传输诊断，没有执行业务工具或计入 Flow 成功。
- **第三轮配置与恢复**：在新独立状态创建同一五节点流程，原样保留 Agent 生成的 SPEC.md继续。仅测试进程的模型客户端工厂使用新的 httpx.AsyncClient(trust_env=False)，保留真实 call_model、全部工具目录和权限/路径边界；工具子进程的代理环境未更改。用户 .env 和系统代理没有修改。需求节点正常完成，后端开始真实实现。
- **RUN-01，自测进程未结束**：后端自测调用留下长驻 app.py HTTP 服务，模型已返回工具调用但命令长时间未回传。测试操作者按 cwd、Python可执行程序、app.py参数和进程start time唯一核对，仅对本轮服务器发 SIGTERM，未修改业务文件，让真实工具结果回到节点。此干预保存在 `selftest-process-intervention.json`；本轮不能描述为全程无人干预。
- **TEST-01，外部验收器误拒**：初版以完整JSON对象比较，误拒合同允许的额外显示/统计字段。新增失败回归后投影必需字段并严格校验类型/范围；保留集合数量、重复项和错误无副作用。
- **TEST-02，外部验收器响应分类**：投影仍靠可选loans字段猜GET/POST类型，合法POST额外返回loans会丢掉必需loan。新增失败回归后按HTTP方法投影，必需字段缺失/类型错误仍失败。验收器聚焦31项通过；这些为受控fixture，不是模型产物通过声明。

- **RUN-02，第三轮取消**：自测下一条命令再次启动有效前台服务，流程仍停在后端。通过真实 Flow cancel API 取消，状态从 cancelling 变为 cancelled；Core 回收该轮工具进程，未绕过预约或伪造节点成功。保留运行、时间线和已生成文件。
- **CASE-01，改进步骤职责**：后端阶段保留实现与编译/函数检查，将 HTTP/并发/重启统一交给质量节点；质量节点明确每请求5秒、Popen try/finally及terminate/wait/kill，测试命令上限120秒。业务验收断言保持。修订后的精确JSON就是可复用用例，原版保存在本轮报告目录。
- **第四轮复验**：新状态载入修订用例，沿用真实Agent已生成的SPEC.md和app.py，不人工补代码。再次从Web界面启动五节点完整串行执行。节点接手时的输入明确说明已有文件，避免称其为空目录从零重写。

- **FLOW-01，实际职责越界**：第四轮需求节点的system消息确实含“只做规格”的完整node.prompt，却创建了三个前端文件与test_app.py。代码链路确认prompt没有丢失或被降级；本轮用户执行消息只含总目标交接和通用“执行当前任务”，没有再次明确当前节点的工作范围。这是观测到的指令遵循/任务表达问题，不能声称是system丢失。
- **改进与复验计划**：保持原system提示、全部权限/工具与交接，Core将在实际执行消息中明确本节点名称、指令和交付范围，避免总目标替代当前阶段；补真实FlowService请求组装回归。提示词不是文件权限或强隔离保证，效果仍由真实运行验证。已取消越界的第四轮，准备全新的空目录复验修复后的五节点，各阶段创建文件与交接单独核对。

- **FLOW-02，消息补强回归**：structured context 开关两种模式的新断言先因用户消息缺少当前节点标题失败；修复后相关19项通过。最终完整非集成回归1725 passed、2 skipped、5 deselected（107.81秒），不是业务产物的验收结果。
- **第五轮，从新空目录复验**：新Project使用project-scoped空目录，配置仍为完整工具目录、模型直连与120秒命令上限，五节点全部从头执行。开始时已核对目录为空并保存测试JSON/Core/验收器源码哈希。模型HTTP客户端只记录session/model、请求字节数、schema数量、max_tokens和时间，不保存请求正文或凭据。浏览器首次因新页面窄视口导致左栏项目按钮不可见，恢复1664px后从真实项目入口点击运行；这次选择器超时记为H-01，不当产品通过/失败证据。

## 最终结果

**第五轮从新空目录完成五个真实 AgentCore 节点，独立业务与浏览器验收通过。** 此轮没有测试操作者终止挂起命令或人工修改业务源码；前四轮的失败、取消和干预仍单独保留。

### 节点分工与交接

| 节点 | 状态 | 实际工具调用数 | 主要产物 |
| --- | --- | --- | --- |
| 01 需求 | completed | 3 | SPEC.md |
| 02 后端 | completed | 22 | app.py |
| 03 前端 | completed | 21 | index.html、static/style.css、static/app.js |
| 04 质量 | completed | 26 | test_app.py、QA.md |
| 05 交付 | completed | 9 | README.md、DELIVERY.md |

五个 session 不同，均绑定同一目录及同一设备/inode身份；四条边的父输出逐字包含在下游输入。每个会话的system与实际user执行消息均包含当前节点指令。10 项独立编排/交接断言全部通过，**81 次工具调用与 81 个结果完全配对**。文件工具请求与执行审计均保存为脱敏摘要，Shell操作不被误称为逐文件安全隔离。

前端按后端入口将页面放在根 `index.html`，CSS/JS 位于 `static/`，实际根页面与资源均通过 HTTP 验证。质量节点在 `QA.md` 记录了新测试自身的错误：test_14停服后立即请求而未重启，初次报 Connection refused；随后修正为同库重启再断言，保留持久化/归还/重复归还断言。父验证程序没有人工补业务代码。

最后一轮持久化run墙钟 **514.560 秒**，74次主模型回调、74次provider请求尝试，模型HTTP客户端观测也为74次，全部 outcome=ok。provider成功口径输入 1,720,464、输出 40,716 tokens，包含跨请求重复上下文，不代表独立文本量或计费成本。未估算价格；独立传输探针不混入这些数字。

### 五轮结果

| 轮次 | 状态 | 需求 / 后端 / 前端 / 质量 / 交付 | 主模型回调 / 请求尝试 | run秒数 |
| --- | --- | --- | --- | --- |
| 1 | failed | failed / blocked / blocked / blocked / blocked | 4 / 8 | 56.275 |
| 2 | failed | failed / blocked / blocked / blocked / blocked | 7 / 11 | 84.528 |
| 3 | cancelled | completed / cancelled / blocked / blocked / blocked | 18 / 18 | 534.011 |
| 4 | cancelled | cancelled / blocked / blocked / blocked / blocked | 23 / 23 | 121.489 |
| 5 | completed | completed / completed / completed / completed / completed | 74 / 74 | 514.560 |

前两轮使用原代理路径；第三轮模型直连但自测长驻，含一次显式测试操作者回收；第四轮120秒命令上限，但观察到需求职责越界后取消；第五轮使用明确阶段执行消息、新空目录、模型直连和120秒命令上限。失败与取消不算通过，也不将恢复后的非空目录称为从零实现。

### 独立业务验证

- 父程序实际执行 `python -m unittest -q`：**20 项通过，退出码0**，独立进程墙钟 1.869 秒；不是仅引用QA自报。
- 外部验收器在自己的数据库上执行完整契约：**999 条检查断言全部通过**。这包括反复的字段/列表/快照核对，不是999个独立业务用例。检查覆盖空库、合法上下界、非法JSON/类型/数量、400/404/409与无副作用、两个并发请求只成功一个、归还/重复归还、实际进程重启、SQLite文件头与只读 `PRAGMA integrity_check`、静态内容/MIME和原始/编码路径穿越。
- 外部验收时及其后的最终 app/前端/test_app 源码哈希一致；最终状态的源文件与打包产物也有独立SHA256清单。

### 实际浏览器验证

- 在生成应用中通过真实表单添加“会议投影仪”5件，借用3件得到201，库存显示2、活动借用1；再次尝试借3件时前端显示“可用库存不足”，数据不变。该分支在客户端拦截，后端409由独立HTTP测试另行覆盖。
- 通过实际归还按钮得到200，库存回到5、活动借用0；已归还/进行中/全部筛选与刷新后的记录和统计正确。
- 在实际 **450×900 CSS px** 视口完成另一个设备的新增、借用和归还，最后库存6/6、活动借用0。环境DPR=1.25，工具viewport需563×1125；记录实际CSS宽度，不能只按工具参数声称450。文档scrollWidth=438，无页面横向溢出。
- 设备名包含HTML标签的探针按普通文本显示，DOM中未插入该img、执行标志保持0；不是全面安全审计。原生Playwright点击正常，没有替换API结果或用DOM.click冒充原生交互。页面脚本异常计数为0；预期的客户端错误反馈和favicon资源错误不被说成控制台零错误。

浏览器驱动问题单独记录：option是隐藏的原生下拉选项，应等attached而不是visible；“借用设备”标签同时命中region和select，改用已观察到的输入ID；归还按钮实际可访问名称包含记录ID，精确匹配“归还”会找不到；客户端库存拦截不产生HTTP响应，不能等待409响应；手机布局尚在变化时3秒稳定等待超时，稳定后5秒原生点击成功。fullPage截图在此DPR下出现裁切，最终采用viewport截图并以DOM尺寸核对布局。这些没有被计为应用缺陷或伪装成通过。

### 回归、产物与清理

- 完整非集成回归 **1725 passed、2 skipped、5 deselected（107.81秒）**；两项跳过为可选ANN extra。
- 新验收器聚焦31项，导入用例3项，Flow消息相关19项；聚焦采用受控fixture验证测试工具/请求组装，不冒充真实provider。
- locked sync web/desktop、真实Bubblewrap+prlimit探针、diff检查通过；AgentTest wheel中的用例JSON、导入模块和验收器与当前源码哈希一致。最终独立只读复核无确认新缺陷。
- 脱敏证据在 `.cache/reports/equipment-flow-20261031-cigh40sc/`：`attempts-summary.json`、每轮progress/timeline/status、`transport-probe.json`、`role-drift-safe-evidence.json`、`flow-independent-summary.json`、`independent-http.json`、`independent-unit.json`、`browser-checks.json`、`verification-final.json`、源码哈希与截图。
- 生成应用源码保存在该目录的 `generated-project/`，并提供 `equipment-lending-generated.zip`。只保留源码、规格、测试和说明，不打包数据库、原始对话、审计、日志或凭据。`QA.md`、`DELIVERY.md`为Agent生成的过程报告，独立验证结果以上述父程序证据为准。
- 本轮自有Web/业务服务已关闭，46254/46418/43330/45034/43826均确认释放，无进程仍使用本轮私有目录。原始私有测试状态已精确删除，用户既有服务、状态和缓存不操作。沿用原分支，保留此前全部工作，本轮尚未提交。

## 重复使用

导出完整用例：

```bash
uv run --no-sync python -m agent_test.scripts.equipment_flow_case
```

在已有 Core 管理器及 Flow/Project 服务中显式创建用例：

```python
from pathlib import Path
from agent_test.scripts.equipment_flow_case import create_case

ids = create_case(flows, projects, Path("/home/user/projects/equipment-demo"))
# 从界面运行项目；或在服务的异步事件循环中调用 flows.start(ids["flow"])。
```

目录必须已存在且满足 Core 授权边界，创建不会清空目录；本轮实际使用新空目录。辅助模块不更改模型、工具、全局权限或环境配置。用例声明的 local/full_access 为此示范模板设置，运行前可由项目设置覆盖。

独立业务验收：

```bash
uv run --no-sync python -m agent_test.scripts.validate_equipment_app /path/to/generated/project --output /path/to/result.json
```

它只运行明确指定的可信项目，使用自有临时数据库，不修改业务代码，不输出服务正文或凭据。该命令通过也不代替真实浏览器验收。

本轮生产改动只补强Flow当前阶段任务的表达，原有system提示与交接、权限、目录授权和工具行为保留。没有新依赖或环境变量；`.env.example`只补充说明，用户`.env`与系统代理没有改动。现有应用进程需重启后加载新的Core消息组装。
