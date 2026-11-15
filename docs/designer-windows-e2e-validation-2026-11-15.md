# Windows 设计助手端到端验证（2026-11-15）

> 状态：进行中 · [文档导航](README.md) · [Windows 指南](windows.md) · [Agent Flow](agent-flow.md)

## 目标与范围

复现 Windows EXE 已配置模型但设计助手首段响应前连接失败的问题，检查连接体验，再用该模型完成从设计、导入、项目运行到模板往返的客服反馈流程。真实模型、正式旧版 EXE、当前源码的受控回归和后续新构建须分别记录；模板生成或节点完成均不等于业务验收通过。

使用独立 Windows 验证根 `D:\AI\AgentSrcCode\ChannelAgent\.cache\reports\customer-flow-win-20261115-389a41b7be`。七条合成公开反馈包括 001、002、003、004、007、008 和 SYN-NO-KB；预期结果不提供给模型。`support` 知识库使用八份公开 Markdown，本轮复制既有公开 embedding 和 rerank 权重至独立 assets，离线运行，没有下载；outbox 仅在项目内模拟，不实际发送。

## 已确认事实

- 原保存连接使用 `deepseek-v4.1-flash`、`opencode.ai` 和环境代理；继承的 `ALL_PROXY` 指向 `127.0.0.1:7892`，TCP 连接被拒绝（10061）。Windows 固定系统代理的 `127.0.0.1:7890` TCP 可连接。
- 同模型真实 Core 短请求：原路由 5.604 秒、两次尝试均在连接阶段失败，无正文；隔离进程仅在内存中选择 `windows_system` 后，3.315 秒一次成功，provider 报告输入 37、输出 22 token。这是短请求对照，不能证明完整流程可用。
- 用户随后明确授权将原「测试」模型改用 Windows 系统代理；通过 `RuntimeModelRegistry` 原子更新，仅 `proxy_mode` 字段改变，更新前后整体比较确认密钥、模型及其他参数保留。原 `.env` 未改，已运行的 0.6.0 EXE 须重启才能加载变更。脱敏证据为验证根内 `authorized-model-route-update.json`。
- 正式 0.6.0 EXE 在独立便携数据根经实际界面调用配置模型，生成并自动导入五节点、六连线模板，导出 `generated-template.json`，24,379 字节，SHA256 `971c86d4eb9a30556ea001d47c41bf977ddbad6416933795e0e7036631bb56b1`。文件格式为 `agent-flow` / version 1；四个动态工具 `read_feedback`、`submit_to_ops`、`send_reply`、`escalate_to_human` 声明网络为 false。生成和导入没有执行工具。静态安全审查无发现：工具使用标准库和固定 outbox，不使用网络或凭据；追加操作非幂等，不能假定复跑自动去重。
- 独立旧 EXE 已通过界面创建并绑定独立项目、选择同一模型并保存，保留 `smart` / `sandbox` 策略。Working 曾显示等待确认及运行状态，但界面恢复运行不能证明工具已获授权执行。

## 首轮真实项目运行失败

运行 `b834bb5a3b4d44749bb8eb731e80fbee` 于 2026-11-15 10:21:24 UTC 开始，分类会话 `03f6661b50934cabb782ebe9d4837cf5` 两次调用 `read_feedback` 均被拦截，报告“原生审计连接已关闭”。审计记录两次均为 `allowed=False`，没有 `command_started`；没有 outbox 副作用。此前将界面恢复运行理解为工具已执行的判断不成立。

模型改用 `read_file` 读取七条反馈，输出中将 CF003/008 判为垃圾、001/007 判为技术、002/NO-KB 判为服务、004 判为人工；这只是模型自报。最终消息在 JSON 代码围栏前后夹带工具失败说明，严格交接校验拒绝“交接输出不是有效的JSON”，四个下游节点 blocked，未运行；不能算业务成功。

定位表明 Host 与 Linux worker 各有 30 秒确认期限，worker 从发送时开始计时较早，先超时删除 pending；Host 随后返回的合法超时拒绝被 worker 当作未知 nonce 拒绝，输入关闭后拒绝审计等待 ACK 时报连接关闭。旧 EXE 对应源码核对了会话、配置和 WSL worker 文件。后续修复采用 Host 单一确认期限，同时保留 worker 总期限、取消和 EOF 处理、未知或重复 nonce 的 fail-closed 与执行前审计 ACK；不延长原确认、命令或退出时限。须保留本轮失败并用原案例完整复跑，不删除契约或手工摘取 JSON 来通过验收。

## 源码修复与回归

当前未发布源码增加无工具短请求的模型连接检查，使用实际连接路由及共享预算、模型槽；不保存会话、不返回模型正文或异常正文，也不使用备用模型。桌面与 Web 显示代理来源和结果，设计失败可进入模型管理；取消和迟到结果处理有对应回归。真实界面发现新图沿用旧图平移使节点移出视野，桌面源码调整为首次打开新图时适配，同图刷新保留用户缩放和平移；Web 已有首次适配逻辑。

排队取消、Web 断连、取消锁重入、模型删除后取消和标签页断连的缺陷先以受控测试复现再修复；最终源码审查无发现。相关聚焦测试 76 项通过，桌面画布修复的 Qt 受控测试三项先失败后通过。这些测试不是实际新 EXE 界面或真实模型业务验收。

初步 Linux 非集成回归为 2501 passed、55 skipped、5 deselected，185.50 秒，记录于 WSL `.cache/reports/designer-full-20261115-xB0gUB/full.log`；该结果早于最后几项修改，不能作为最终源码全量结果。

运行中发现动态工具确认仅显示不透明的 base64 runner；后续补充框架可信的可读名称、参数、网络和代码元数据，保留既有风险、命令、审计和确认，并收紧 WSL 绑定元数据与完整预期提示的匹配。另修复桌面旧确认被替换时完成信号误报旧 ID 的红色提示。这些修复的成品验收尚未完成。Windows 首轮整组因测试 harness 将临时目录放在源码 `.cache` 内触发保护边界，结果为 73 failed、876 passed、80 skipped（279.48 秒），不能作为有效全量；报告保留在 Native `.cache/reports/native-designer-20261115-182131-1146f68d/`。三项分发测试的版本夹具已调整为读取项目版本，独立文件 13 项通过，生产构建器和原断言未改。

Linux 后续全量在源码 `582a85c` 为 2532 passed、56 skipped、5 deselected、1 failed（243.11 秒）；唯一失败是 0.6.1 更新日志缺少带日期的标题，现已按发布检查器格式补充。该轮也早于正在进行的审计修复，不能代表最终源码。

随后原日期失败和两个 POSIX 参数用例定点 3 passed（0.23 秒），不能与上述全量拼成通过结果。Native 完整选择集 965 passed、90 skipped、2 failed（542.08 秒）：两个受控 POSIX 夹具在 Windows 误走真实 WSL，系统 Python 缺少 `dotenv`。夹具局部替换命令模块的操作系统对象后，原失败、发布检查及完整 Web Flow 定点 39 passed（14.30 秒）；原断言和全局系统状态不变。原 20 秒退出用例分别 5.446、5.021 秒通过。上述失败保留，不追认为整组通过。

真实 Native 与 Ubuntu WSL 的九项验证通过（83.31 秒，Native `.cache/reports/native-wsl-final-20261115-3d5f6d62/`），包括延迟拒绝、拒绝审计无副作用，以及下一次允许时审计先于执行标记。最终沙箱审查对确认竞态与元数据修复无发现。

## 真实 Web 界面与往返

源码 `3ed5a28` 的远端完整 CI 已终态成功：Linux 2529 passed、61 skipped、5 deselected（252.14 秒）；Windows 1000 passed、58 skipped（138.48 秒）。这是该提交的回归结果，不覆盖后续改动，也不代表真实业务验收。

Native 源码 Web 服务使用独立 `web-data`，在实际浏览器中从无会话入口打开模型管理；同模型通过 Windows 系统代理的真实连接检查约 5.0 秒成功。808×883 和 416×900 界面的 DOM 检查未见横向溢出，按钮位于对话框内；截图保存在验证根 `screenshots/web-models-success-wide.jpg` 和 `screenshots/web-models-success-416.jpg`。这不是新 EXE 的验证。

桌面导出文件经实际 Web 文件选择导入，再从 Web 导出，JSON 语义相等，均为五节点、六连线和四动态工具；`web-roundtrip-template.json` SHA256 为 `32b1be45c989dbe2c241ad708cbac7771f89701e4d1a2be05b5dbaf5dec53d60`。该往返没有运行项目或验证业务结果。

受控 Chromium 检查 41 项使用模拟五节点，不能当作真实模型结果；工具栏样式已审查并提交，实际界面复验未完成。用户本轮再次按物理 Esc 停止 Computer Use，随后明确选择「仅继续不操作桌面的工作」；因此不再操作桌面或现有浏览器，可见界面验收保留为未验证。此前停止记录保留，不能用内部继续工作替代界面授权。

真实本机 HTTP 取消探针验证显式取消返回 499（约 0.328 秒），另一请求断开后同一模型的新检查成功（4.125 秒）。初始探针将预期 499 错当失败，保留其脚本，修正探针断言后通过；未修改产品响应语义。证据为 Native `.cache/reports/model-http-cancel-20261115-dd68b28c/`，这不是浏览器关闭或 GUI 验收。

## 后续非 GUI 业务验证

独立非 GUI 真实模型预验已完成一轮，仍失败：运行 `1715bc91ba56497da623c458cf180a61` 用时 68.428 秒，原七条输入及五节点契约保留。`read_feedback` 经限定的测试授权后真实执行，审计 exit 0、收据七条齐全；两次模型调用正常结束。最终正文在 JSON 围栏前后分别带有 532、103 字符说明，严格校验拒绝，四个下游 blocked；RAG、其余三工具及 outbox 均无业务执行。该轮不是 GUI 验收，也没有重试或放宽契约；独立证据为 Native `.cache/reports/customer-headless-20261115-192008-8ec68004/`。下一步定位模型最终输出协议，保持严格验收。

后续源码在同一会话任务、原模型/轮次/费用预算内加入一次禁工具的格式纠正，保留严格 Flow 验收与原答复，拒绝无效结果进入长期记忆。首轮全量为 2556 passed、56 skipped、5 deselected、1 failed（233.19 秒）：新增可选参数被无条件传给原本只接收一个参数的受控 `_run` 替身。已让无契约路径继续按原调用签名执行；原用例及相关聚焦 53 项通过。新功能与契约的受控回归为 38 passed，代码审查无发现；全量将以最终源码重跑，不能拼接为整组通过。

源码 `3e18332` 的远端完整回归终态成功：[运行 43710299002](https://github.com/vukencc/Channel-Agent/actions/runs/43710299002)，Windows 1028 passed、58 skipped（138.93 秒），Linux 2557 passed、61 skipped、5 deselected（248.73 秒）；真实隔离探针通过。以下真实业务失败不因 CI 成功而忽略。

该源码的下一次独立原模型运行 `71f62703820241bc8a82c1909abed893` 用时 241.600 秒，前三节点完成且严格验收通过，分类节点真实触发的一次格式纠正成功。三次获准动态命令均真实 exit 0，十个调用/结果完整配对；一个额外通用 shell 请求被拒绝，未执行。运维 outbox 有 CF-001、CF-007 两条模拟工单；服务节点随后在 16,395/16,000 估计 tokens 处保存上下文检查点，人工节点 blocked，RAG、回复和转人工工具均未运行。未重跑该实例。证据为 Native `.cache/reports/customer-headless-finalized-20261115-193719-bab5eaae/`。

预算诊断发现框架冗余：严格交接的 ASCII 转义将同一业务 JSON 从 1404/2917 字符膨胀到 5029/14012 字符；节点职责和契约另在 system 与当前输入重复。源码 `a7c110dbbb5eb5810c72ea39d42745b3238d301c` 仅在模型输入中将已严格验证的规范 JSON 转为紧凑 Unicode，保存的规范内容不变；当前节点指令仅完全相同时去重，复用节点指令变化时保留当前完整指令。不增加原 16,000 tokens、64,000 字符限制，不丢弃最新工具结果或业务字段。相关聚焦 150 passed（4.57 秒），审查无发现；Native 与 WSL 源码均已同步并推送。

同模型的独立 `a7c110d` 非 GUI 真实运行 `f073f3e717b2449d94329b81cb2158f5` 在 400.475 秒后失败，服务节点再次进入上下文检查点。真实 RAG 查询 normal refund 返回 3 条、normal no-KB 返回 0 条、loose 返回 8 条但无关；主模型 19 次调用，21 组工具调用和结果配对，三次动态命令 exit 0、三次通用命令被拒。七条路由均正确，但 CF001 的 high 与预期 critical 不一致；回复和人工节点未运行。证据位于 Native `.cache/reports/customer-headless-context-20261115-195908-381de817/`，不能算业务通过。

`a7c110d` Linux 本机完整回归 2565 passed、56 skipped、5 deselected（232.13 秒），报告 `.cache/reports/flow-context-full-20261115-So16p1/`；[远端 CI 43711474196](https://github.com/vukencc/Channel-Agent/actions/runs/43711474196) 终态成功，精确数量待补。Native 额外选择集为 7 failed、140 passed、3 skipped（183.15 秒）；两处夹具修正提交 `8cd1dd8` 后，相关两文件 32 passed、3 skipped（40.03 秒），报告 `native-fixtures-green-20261115-4217b141`。Windows 1314 符号链接跳过不算通过，定点结果不能拼成完整选择集通过。

Native 队列测试原 5 秒期限、9 个回调在 6 次运行中均超时；受控性能记录中 WSL 3 次为 0.15093 ± 0.02735 秒，Native 约 6.27 秒为截尾测量，不提高期限。后续 `2f2526e` 原 5 秒队列复验仍 3/3 超时，5.8439675、6.2215696、6.2763029 秒是整个调用的截断时间，不是完成延迟。远端 CI 成功不能抵消本地失败；后续复验见下文。

Native 与 WSL 源码 `0969106` 已推送至草稿 PR #54。该提交包含 `read_file` 可信正文注释、单次恢复提示、预算计量过滤元数据并保留 reasoning，以及单次会话提交的祖先 pin。WSL 本轮唯一报告 `.cache/reports/observation-commit-focus-20261115-kHWEyx/` 记录聚焦 **172 passed（3.73 秒）**及真实 Bubblewrap/prlimit 隔离探针通过。独立审查无发现，包含 metrics 37 项通过及先前 49 项通过；这些不是完整业务验收。Native memory-fit 最初 10 项失败源于测试 harness 将 pytest 工作区置于应用根内；移至同级目录后 10 passed（11.13 秒），保留原失败边界。

`0969106` 原生专项为 **3 failed、86 passed、1 skipped（27.73 秒）**。三处均为新增夹具的平台假设：替换 Windows 模块时丢失实际原子写盘函数、Linux 专项没有跳过 Windows，以及文本样本被默认转换为 CRLF。`3916987` 保留真实写盘后端并显式覆盖 LF/CRLF，Linux 定点 22 passed（0.19 秒）；Native 已快进至该提交，原安全专项重验 89 passed、2 skipped（26.69 秒），其中跳过一项 Linux 专项及一项真实 Windows 1314 符号链接限制，报告位于本轮独立临时目录 `native-safety-3916987-b69c45e558`。此前 [CI 43722542611](https://github.com/vukencc/Channel-Agent/actions/runs/43722542611) 保留相同三项失败：Windows 3 failed、1152 passed、58 skipped，Linux 2630 passed、69 skipped、5 deselected。修正后 [CI 43723011743](https://github.com/vukencc/Channel-Agent/actions/runs/43723011743) 双平台成功：Windows 1155 passed、59 skipped（153.05 秒），Linux 2631 passed、69 skipped、5 deselected（253.19 秒）。本机 WSL `3916987` 全量 2636 passed、64 skipped、5 deselected（233.83 秒），报告 `.cache/reports/portable-flow-full-20261115-91iaJS/`。

`0969106` 本机原五秒队列三次仍全部超时，每次只有 8/9 个回调，超时前磁盘仍为 running。whole-call 为 5.5388635、6.6223070、6.2954804 秒，均值 6.152217、样本标准差 0.555748 秒；包括初始化、截止与退出，是截断计时，不证明加速。报告位于本轮独立临时目录 `native-queue-0969106-b9c75e80b2`，原测试摘要及断言未改。

独立非 GUI 真实原模型运行 `15002cfaee8941caa648e7667def4129` 用时 379.955 秒，前三节点严格通过，服务节点在 16,635/16,000 估计 tokens 处进入上下文检查点，人工节点 blocked。19 次主模型请求、23 组工具调用/结果配对、4 次动态命令 exit 0、2 次通用命令被拒；七条路由正确，但 CF001 的 high 不符预期 critical，AI 回复及人工处理未完整完成。证据为 Native `.cache/reports/customer-headless-memory-20261115-212343-72d5e2da/`。

该轮共享记忆从 1,221 字符缩减至 583 字符，第五次服务节点请求成功，所比较的记忆文件 revision 3 和哈希不变；这是实际模型请求证据，不代表整个业务通过。两次 RAG 正文压缩仅出现在随后失败的上下文构建中，不能称为压缩后成功发送。后续原案例复验见下文；尚未构建最终 EXE。


最新 `0969106` 独立非 GUI 真实原模型运行用时 408.443 秒，前三节点严格通过，服务节点在 18,528/16,000 估计 tokens 处进入检查点，人工节点 blocked。17 次主模型请求、21 组工具调用/结果配对，五次动态命令实际执行（读取 1、提交运维 2、发送模拟 AI 回复 2），三次通用命令被拒，RAG 三次，转人工零次。业务检查 12 项满足、8 项未满足或未到达，后者不是八个独立缺陷；CF001 的 high 仍不符 critical，CF002 退款语义未充分核对“所有者”条件，不能宣称政策全部正确。证据为 Native `.cache/reports/customer-headless-readturn-20261115-220704-948a1c8a/` 的安全摘要；不是回放、界面或 EXE 验收。

该轮一次旧正文周转节省 654 估计 tokens、1,661 字符，以 15,873 tokens 构建并实际成功发送；随后的失败构建从 20,204 降至 18,528，三处正文压缩未成功发送。最新新增 reasoning 约 2,772 tokens，而先前成功构建的 reasoning 为 264；不能断言仅压缩旧思考即可解决。工具 schema 约 5,849 tokens，system 与当前 user 合计约 6,574 tokens，均为较大固定开销。

下一方向为设计助手提示词默认按节点职责显式生成 `tool_names`，仅选所需基础工具及业务动态工具；用户明确要求继承时仍用 null。既有模板、用户工具配置、运行时、预算、推理及五节点四动态工具业务范围不变。新辅助流程仅准备设计，尚未调用模型，不能称新模板通过；上述旧案例失败保留。

较早 `3ed5a28` 包的静态审计通过：ZIP 228 项 CRC、227 项清单大小/摘要与全部侧车一致，130 应用模块及 329 资源与对应提交一致；选定九个 PE 为 x64，运行库随包，无 CLR 或所查导入缺失。EXE SHA256 `110008f74056e1d62af9e8f020d815ae22bc2fa0b9e7649b7980e9febf84b340`；ZIP SHA256 `4589d0568a8c37a01efdce9fd0a4e27d4713fc88d698d030db8a9c02c1e580fe`。三项可编辑安装元数据含构建来源路径，未发现它们构成运行依赖；不等同于干净虚拟机兼容性或穷尽二进制敏感信息审计。该包仍非最终成品。

## 待完成

- 完成隔离项目中的七条业务案例，分别核对节点状态、项目产物及业务预期。
- 继续验证 Web 界面其余操作及新构建 Windows EXE；记录真实模型、受控回归和任何失败。
- 较早 Windows 候选包基于源码 `3ed5a280`，已完成冻结、单 EXE 默认完整 15 项自检及上述静态审计；不包含后续交接格式修复，不能作为最终版本，最终源码须重新构建。
- 验证新设计方向并运行新的业务案例；已提交源码的 Native 专项、WSL 全量及隔离探针结果见上文。原窗口仍无法由工具恢复，未强制关闭；原模型代理已按用户授权保存，旧进程须重启加载。
