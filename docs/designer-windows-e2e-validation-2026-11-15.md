# Windows 设计助手端到端验证（2026-11-15）

> 状态：进行中 · [文档导航](README.md) · [Windows 指南](windows.md) · [Agent Flow](agent-flow.md)

## 2026-11-16 当前状态

[PR #54](https://github.com/vukencc/Channel-Agent/pull/54) 已合并，[0.6.1 正式发布](https://github.com/vukencc/Channel-Agent/releases/tag/v0.6.1)；合并提交及注解标签指向 `55472e91ae19c5fbfd6421dca5c2bcfcf985dfeb`。[正式运行 43857331513](https://github.com/vukencc/Channel-Agent/actions/runs/43857331513) 六项任务成功，Release 于 2026-11-16 11:34:46 UTC 发布，共六项资产。用户禁止桌面和浏览器操作，可见 GUI 未验；用户便携根已仅将 `ENABLE_STRUCTURED_CONTEXT` 设为 true，旧应用未重启，新进程生效。

- Windows 正式资产独立审计无发现：实际单 exe 离线自检 16/16、退出码 0；134 应用模块、97 Core、227 清单文件、12 静态和 224 便携资源匹配，289 PE 无 CLR，选定 Python / Qt / VC 导入缺失为零。四项 Windows Release 资产实际下载、SHA 侧车和正式 artifact 逐字节一致。正式 exe 331251961 字节，SHA256 `668e978ee01333bb3584229504cf1ce1ee3a2fff90b03519fc157af1e42d1266`；ZIP `ChannelAgent-0.6.1-windows-x64-20261116-113223.zip` 329506614 字节，SHA256 `3071b8484f59f88a2b0781f767afb908c5500f3f656217cfc0fb63e7551e5188`。审计报告 `sdk-formal-061-audit-7d1b14cbe2514121909b94a04dbf7040`；首次 artifact 下载 EOF 及续传保留。正式包未重复候选的搬移保配置测试，相关源码一致。
- 正式 exe 和侧车已复制至 `D:\AI\AgentSrcCode\ChannelAgent\dist\official-v0.6.0\ChannelAgent-0.6.1-windows-x64.exe`，目标哈希一致，旧 0.6.0 与用户数据未动。Linux 正式 tar 502820250 字节，SHA256 `ab296470f3352344c81cd99c6f6489f458537cf10e5536a5e4bc00d41c05e18b` 与实际侧车及 API 匹配；8530 个 tar 成员安全检查、6759 文件清单大小和哈希通过，134 应用模块及 14 静态辅助资源与 554 源码匹配，三模块均 0.6.1，构建 glibc 2.35。实际冻结程序离线自检 17/17、退出码 0（9.051 秒），含真实 SDK 离线构造、Linux 沙箱和安装边界；无模型调用。4970 文本的常见凭据前缀和私钥标记扫描无候选，不代表覆盖所有编码；Torch 2840 字节无参数示例和 certifi 公共 CA 单独分类。artifact 首次 EOF、第二次取消，未完成 artifact 字节比较；正式 Release tar 已完整下载、核哈希并运行。报告 `linux-formal-061-audit-e803251ac7d64f6da752f3e3b2848b01`。

- 用户便携根的配置更新仅设置 `ENABLE_STRUCTURED_CONTEXT=true`：实际应用退出码 0、published=true、uncertain=false，随后 dry 核验 ok=true、changed=false；原配置内容未输出。脚本先设置私有 ACL，写入并 flush/fsync，重核原文件身份和字节后原子替换并安全回读；非协作并发写者仍有 CAS 残余风险。旧应用未操作或重启，其他用户项目、会话、知识库和旧 exe 未动，新进程启动后生效。安全记录 `portable-archive-prepare-8608aca8e7fa4ad9bc66d9d7fea02cad/actual-update-verification.json`。

- 真实案例 `customer-scope-fresh-run-20261116-3399876a9df2` completed、正常排空，758.594 秒，五节点均严格完成；各节点模型调用 3 / 6 / 4 / 17 / 15，共 45，回执 2 / 7 / 5 / 21 / 21，共 56。原 QA 仅运行一次，19 passed、1 failed：CF001 critical 实际为 high。原 false 保留；README 只定义候选优先级，未唯一规定替代方案未知时必须 critical。实际 reason 保留单租户自述、全项目 503、替代方案未知及高危至严重候选待复核，不能视为最终确认 high 或扩大范围。
- 主业务闭环独立审查通过：退款政策与来源正确，退款两次、NO-KB 三次 RAG 均为 support，无超时、超限或顺序问题；NO-KB unresolved 转人工 false，CF004 为 true，无额外 CF002。两次运维、四次回复、两次人工共八次模拟写入的参数、回执、outbox 和哈希逐值一致，无外发或重复，spam / injection 无交付，反馈不变。原 QA 的 false 与主业务结论分别保留，不能称所有语义或原检查全绿。
- 两项非阻断 P3 待办：NO-KB“已转人工/商务渠道跟进”未限定模拟，不代表真实外发；CF004 客户已报告开票状态而 summary 称未提供，应区分已报告和未核实。均未宣称修复。事后 manifest 显示 97 Core、HEAD、harness、模板及控制文件不变，Native 干净。
- 8034 的 service 98 次调用、总 1481.828 秒，与本轮 service 17 次、总 758.594 秒仅为实测对照；模板、模型随机性和负载不同，不能归因于单一改动。连接、长循环及关键交接已发现的功能阻断已解决，正式发布及 Windows 包验收见上，可见 UI 尚未验。
- 上一真实案例 `customer-ledger-fresh-run-20261116-8034af137568` 终态 flow_failed，1481.828 秒、正常排空，原 QA 18/20：human 未严格完成且人工队列多出 CF002。五次 RAG（退款 2、NO-KB 3）均为 support，无超时或超限；真实顺序为两条技术回复、退款两次检索和成功发送、NO-KB 三次检索和发送。oracle 已正式撤回此前跨快照顺序违规判断。
- NO-KB 正确列入 unresolved 并转人工 false；CF004 原失败及精度 true 正确，技术和退款政策据实。九次本地模拟写入（2 运维、4 回复、3 人工）的参数、回执、outbox 与哈希一致。唯一业务 P2 为 CF002 仅询问政策且已充分回答，却因未询问金额、到账或独立订单核验被额外列入 unresolved 并转人工；human 三次工具调用合批及回执触发检查点。事后 manifest 核对 97 Core、HEAD、harness、模板及控制文件不变，Native 干净。
- 离线测量 `handoff-window-measure-9896d62d960c4ade81332893c88a8677/priority-human-counterfactuals.json` 使用尺寸匹配载体重建，缺少历史 runtime 全文，不是完整 wire 精确回放。fixed 不含新批次为 8411 tokens，三次调用与结果 6417（含 reasoning 2108）；原 14828 加预留 2000 超过 16k 828。保留全部 reasoning，仅移额外 CF002 配对则余 701；仅省精确长输入回显余 648；两者同时余 1652。可重读 Handoff 引用方案估计节省 human 3068 / service 3127，但属中等范围、非本案必要，未实现；测量未改实际产物、预算或测试。
- 9559867 仅增加设计助手通用 scope 指导，聚焦 53 passed。[PR CI 43851760114](https://github.com/vukencc/Channel-Agent/actions/runs/43851760114) 双绿：Linux 2815 passed、71 skipped、5 deselected（254.68 秒），Windows 1350 passed、68 skipped（241.35 秒）；push 43851746604 亦双绿（Linux 247.79 秒、Windows 203.12 秒）。
- 新真实 xhigh 设计 32d257 一次调用 106.227 秒，仅追加两个 prompt，但 human 旧前缀 offset 20 多出两个换行，严格拒绝并保留原证据。cdb60d 本地仅删除两个换行，27/27 与两次真实往返通过，无新模型调用；派生模板 SHA256 `9b968d851b08323434c2cb413702f8c4d925a521156f17325916acc10431f1a5`，oracle 无发现，不能称模型原样通过。
- 新账本增加最多 900 字符上下文，计入原 16k / 64k 预算，并非提高输入预算或硬执行限额。聚焦回归先红后 72 passed；O(N²) 裁剪与 surrogate 两项 P2 均已修复并回归，最终审查无发现。Native checkout 专项 `native-ledger-checkout-20261116-d51d1652b6b3` 13 passed，源码和测试哈希不变。
- Linux 最终全量 2818 passed、68 skipped、5 deselected（223.68 秒），真实隔离探针退出码 0、Core 哈希不变；报告以 `ae31212` 标识。[PR CI 43845518827](https://github.com/vukencc/Channel-Agent/actions/runs/43845518827) Linux 2815 passed、71 skipped、5 deselected（251.39 秒），Windows 1350 passed、68 skipped（180.21 秒）；push 43845330735 亦双绿（Linux 244.10 秒、Windows 231.61 秒）。
- xhigh 设计 `customer-evidence-ledger-design-20261116-a00a5ce83be8` 一次调用 102.1 秒，严格原样校验失败：human 旧前缀 offset 677 插入两个换行。拒绝证据保留；随后 df32 本地仅修复这两个字符，26/26 与两轮往返通过，没有新增模型调用，得到派生模板 11bb17，独立 oracle 审查无发现；不能称为模型原样通过。
- d342 候选 only-exe 自检 16/16、exit 0，独立包审计通过，但不含当前源码，不能作为最终 EXE。既有原 5 秒队列三次中一次失败仍保留，不能由其他成功结果抵消。

## 迭代记录（当时状态）

以下保留各阶段证据；“运行中”“待验”和源码状态均指当时，不代表当前结论。

- 5b62412 修复工具超时后工作线程尚未进入工作区锁时的取消竞态：仅框架明确的锁取消返回原超时，身份、执行及严格审计失败继续传播；外部重复取消等待真正排空后传播。确定性回归先失败后修复，双 guard 覆盖通过独立审查。Linux 相关 75 passed；最新生产 Core 全量 2805 passed、68 skipped、5 deselected（232.21 秒），真实隔离探针退出码 0，97 个 Core 哈希不变，报告 `final-timeout-drain-full-20261116-bd136c2299a54fdfb8870d28b1204bf0`。
- 新排空夹具 Native 首次为 13 failed、4 passed：20ms 期限在到达受控门控前已取消，原三个实际超时参数通过。5219bbc 仅调整测试级同步，受控线程到位后仍调用真实 20ms 超时，原三个实际时限及所有断言不变。Native checkout 专项 `native-timeout-drain-checkout-20261116-d0d2b2426fbb48f7a69c79d281823cfd` 为 17 passed、0 skipped，前后 HEAD、97 Core 和测试哈希一致；独立审查无发现。这不是原五秒高频轮询性能问题的修复。

- 最新真实案例 `customer-evidence-locator-run-20261116-d0748bcdc5c5` 终态 completed，1349.609 秒，五节点严格完成；triage / tech / ops / service / human 模型调用分别 4 / 9 / 4 / 57 / 11，回执 3 / 9 / 6 / 84 / 14。原 QA 16 passed、4 failed：人工 ID、NO-KB 先前失败事实、NO-KB 未解决状态及真实 support RAG 调用。NO-KB 的 false 标记表示缺少记录，不能称为捏造失败。
- 七次实际写入（两次运维、四次回复、一次人工）参数、回执、outbox 和哈希逐字段一致，均为本地模拟，无外发或重复；缺少第八次 NO-KB 人工交接。独立审查发现退款 RAG 四次、NO-KB 六次均超过三次上限（空来源和超时也计），首次 source 为空导致查错库；退款未闭环先查 NO-KB。回复称待人工，但 service 的 unresolved 为空，human 因此只处理 CF004。CF002 退款政策实际 RAG 语义通过，CF001 critical 候选与 unknown 符合原事实，CF004 真实失败步骤及精度标记通过；证据状态措辞和“客户要求人工”与权限范围并非同一问题。
- 第 140 条消息实际读取 outbox 回复文件 offset 0、limit 6000，第 141 条明确包含 NO-KB 待人工信息；最终遗漏属于模型检查问题，不能主要归因于正文未交付。工作索引未保存 RAG 累计次数，但原 querySummary 可恢复，事实未丢。最终 source-after manifest 对照 97 Core、HEAD、harness、模板及控制文件未变。当时 WSL 未提交的账本改动追加固定 900 字符并与既有 1800 字符并列，计入原 16k / 64k 预算；先红后 64 项聚焦通过，当时审查发现 O(N²) 裁剪问题。不保证模型服从，不增加业务硬编码、硬限制或新 schema。
- 当时真实 xhigh 设计 `customer-evidence-ledger-design-20261116-a00a5ce83be8` 正在运行，基于 9ff5 仅追加三个 prompt，指导 support 来源、每事项累计三次检索、逐项闭环、资料缺口和最终 unresolved 与证据精度；尚不能记为通过。
- [PR CI 43838251690](https://github.com/vukencc/Channel-Agent/actions/runs/43838251690) 在 5219 双绿：Linux 2802 passed、71 skipped、5 deselected（249.91 秒），Windows 1337 passed、68 skipped（251.54 秒）；push 43838243082 亦双绿（Linux 254.11 秒、Windows 199.16 秒）。此前 5b 的 CI 43837190130 Windows 2 failed、1335 passed，新 20ms 夹具未到 barrier；5219 仅同步修正夹具，保留失败证据。
- d342 候选包独立审计 `sdk-candidate-audit-20261116-dce87df3f9e84302b3c15d06043dfe41` 的安全摘要记录 ZIP CRC、清单、134 应用模块、97 Core 和资源匹配，搬移后配置保留；首次实际 only-exe 离线自检报告 16/16，但退出码未采集。随后同候选重新运行记录 exit 0、16/16（167 秒并发，不作速度推断）。这不是包含 5b 的最终源码包，也不是正式发布；最终包仍待验。

- Native 报告 `native-startup-history-final-20261116-bcdddbecc869`：97 passed、0 skipped、0 failed（98.475 秒），源码哈希不变；cp936、UTF-8 mode 关闭，SDK MockTransport 两次调用，无真实网络。两项原 20 秒用例分别 5.465 / 3.096 秒，原 5 秒队列用例 2.651 秒通过。`flow-queue-lazy-sdk-20261116-782555e8c18047afb1b278631904277f/after` 三次原 5 秒队列测试为 1 failed（7.95 秒）、2 passed（4.61 / 4.82 秒）；进程墙钟 10.236 / 6.604 / 6.666 秒不是完成时延。此前 1261、967、e0 原期限超时；这些结果不证明无限稳定或单因素归因。
- 真实模型 `customer-first-delivery-run-20261116-4863601ad200`（Native 5f22、Core 543）：1799.75 秒正常取消并排空，原检查 14/20，业务未通过。退款正确；技术 outbox 将无原文支持的“客户自述无替代方式”写成事实，NoKB 检索 5 次超过 3 次限制，服务未闭环便切项，人工未完成。52 批首次交付 prepared 一致；历史恢复从 offset 0 读取推理，未触及 RAG 原文，原始数据未损坏。
- f28e4ec 的历史搜索可选 `tool_name` 匹配真实调用配对，`read_args` 按 JSON 字符 offset 分页；默认搜索兼容、原文不变，不因此启用 reader 权限，26 项回归。SDK 延迟加载保留真实模型入口，纯 Pydantic schema 与锁定 SDK 严格字典一致，时限、权限及原子性不变；旧诊断 0xC0000005 根因未知，不能称已修复。d342400 扩展 packaged_smoke：真实 SDK 离线构造、关闭和类型及动态 proxy 模块验证（handler 零次调用），自检由 15 项增至 16 项，源码专项 20 passed，尚未冻结 EXE。
- 5f22 的 [CI 43821208737](https://github.com/vukencc/Channel-Agent/actions/runs/43821208737) 与 [CI 43821216760](https://github.com/vukencc/Channel-Agent/actions/runs/43821216760) 双绿：Linux 2754 passed、71 skipped、5 deselected（253.56 秒），Windows 1289 passed、68 skipped（184.15 秒）。后续源码的 CI 与新 EXE 仍待核验。下一 xhigh 模板限域追加三个 prompt 和 README 前瞻 SOP 正在准备，尚未验证。

- 只读诊断 `queue-residual-light-20261116-eeaaf31a258440fd9e3ec95d3de55279` 单轮原 5 秒通过：133 次 get 累计 2.954 秒、Flow 写入 1.088 秒，9 次 drain 合计 0.437 毫秒、busy=false，loop lag 324 毫秒；并非旧失败的精确复现，不能单因素归因。
- WSL d342 全量报告 `.cache/reports/final-lazy-sdk-full-20261116-ef1ae1acb853443792c53580fcd6f8d0`：2791 passed、68 skipped、5 deselected（232.40 秒），97 个 Core 源哈希不变，真实隔离探针退出码 0。f28 的 [PR CI 43832539572](https://github.com/vukencc/Channel-Agent/actions/runs/43832539572) 双绿：Linux 2785 passed、71 skipped、5 deselected（244.71 秒），Windows 1320 passed、68 skipped（148.16 秒）；其 push 运行 43832532272 的 Linux 为 1 failed、2784 passed，原 tool timeout serial guard 线程在超时取消后才启动并抛 SandboxError，新 42 项修复仍在安全审查，尚未提交。
- d342 的 push 43834960767 和 PR 43834970796 双绿，数量待核。手动候选发布运行 [43835112398](https://github.com/vukencc/Channel-Agent/actions/runs/43835112398) 首次 Windows 原 background job 夹具 `entered.wait` 两秒超时，1 failed、1322 passed、68 skipped（313.39 秒）；Linux 成功，打包跳过。已按原代码和期限申请一次失败任务重跑，不能记为发布或最终 EXE。
- 新 53c425 模板一次真实 xhigh 调用 87.418 秒、25 项通过，但独立审查发现服务节点两处指令歧义。82c 业务在 triage 三次调用后由父进程于 50.765 秒正常取消并排空，未到受影响的服务节点，不能记为业务通过。
- 更正设计 `customer-evidence-locator-design-20261116-95d21368ae5b`：一次真实 xhigh 调用 101.495 秒，25/25 检查及两次真实往返通过，模板 SHA256 `9ff5cfd7d7e34688a92a2c12c60336bddf749f89fbd12f47093decec8b60b1a0`。相对 433f 仅追加三个 prompt，其余字段逐值不变；独立复审无发现。项目 README 前瞻澄清的新输入 SHA256 `5475e24bf375e57421fc1883353c96571361ecaa770b2dd2b764d68d7b3a59ea`，原七条反馈、八份知识库和 oracle 不变，不能追认旧案例通过。

- b017cad 修复私有历史 junction 越界；f0c2a52 的 [CI 43791493510](https://github.com/vukencc/Channel-Agent/actions/runs/43791493510) Linux 2655 passed、70 skipped、5 deselected（257.69 秒），Windows 1188 passed、67 skipped（136.52 秒）。更新的 [CI 43795657856](https://github.com/vukencc/Channel-Agent/actions/runs/43795657856) 已成功，数量尚待核对。原用户 .env 的归档开关不存在，默认 false 且尚未更改；安全新 EXE 到位后按用户选择配置。受控案例单独启用结构化历史。
- 首次新受控历史真实案例 customer-receipt-retry-structured-20261116-002053-a2d1f3c3 在 f0c2a52 上 0.516 秒即失败，request_attempts=0，ModelProxyError，没有 HTTP 请求。随后 f50c37c 尊重 Windows 手动代理关闭：当前注册表关闭且无 PAC/自动检测时直连，不继承失效环境代理；启用但无效的代理、不可用的 PAC 或自动检测仍失败。两平台各 30 项聚焦通过，原模型的显式代理选择不变。
- 新 Native 真实模型案例 customer-system-route-20261116-95c91be128 使用 f50c37c，五节点、六连线、四工具，16,000 tokens / 64,000 字符、medium-auto、模型 90 秒及确认 30 秒、smart/sandbox。前三节点严格完成；服务节点 26 次模型调用、48 条工具回执、RAG 23 次、发送 0 次，人工节点 blocked。前两次 RAG 120 秒超时，后续检索可用但反复读取。父进程通过原停止文件触发 Flow.cancel，等待后依次关闭 Flow、Manager 和 store，609.562 秒成功结束，磁盘状态 cancelled，非强杀。11 项检查满足、9 项不满足或未到达，后者不是九个独立缺陷；原反馈未变，运维模拟操作各一次，无实际发送。没有证据证明归档遗漏的代码根因已修复。
- 较早的 11 月 15 日 b017cad 真实案例强制停止于 902.429 秒，磁盘仍为 running，未确认排空；53 chunks、18 revision 的原始校验通过不等于业务通过。真实 xhigh 设计改进 customer-ordered-design-20261116-7b6f1a2d 随后以一次调用、71.080 秒完成，10 项校验通过：只向服务节点追加逐项工作顺序，原提示完整保留，其他字段不变，两轮 FlowService 导入导出等价。新模板 SHA256 为 117b2578ab1877b21d272e98fc5b48cc7514d6380e8ad3cdc9c4a9994520f039。
- 该模板在 0234ee8 独立真实鲜跑 customer-ordered-run-20261116-3fa6c002d5：前三节点严格完成，服务节点发送两条技术模拟回复后继续读取；最终 24 次模型调用、37 条工具回执、RAG 4 次、history_search 2 次、发送 2 次。父进程正常取消并排空，用时 600.532 秒；磁盘 cancelled，人工阻断。原业务检查 12 项满足、8 项未满足或未到达，整体未通过，没有提高预算、降低推理或修改验收。
- c82be1c 将 WSL 路径映射按一次命令分批并反向核验，原 20 秒 Flow / 8 秒标记期限及断言保留。Native 首轮 21 项为 17 passed、3 项 UTF-8 夹具失败、1 skipped；c3cf16c 仅修测试编码后，最终 11 文件 141 passed、1 skipped（154.56 秒），报告 native-guidance-path-final-20261116-18c2b6576eb444ee9deac962c42d83c2，临时目录在应用根的同级目录。WSL c82be1c 全量 2672 passed、67 skipped、5 deselected（241.27 秒），报告 system-route-path-batch-full-20261116-i0skUi；哈希未变，真实 Bubblewrap/prlimit 探针通过。批处理只读安全审查无发现，另有 10 项测试及 Linux 实际特殊字符 WSL 路径检查通过。
- 3894dfc 修复一处已复现的上下文冗余：只暴露读取和业务动态工具时，仍注入完整 CRUD、命令和安装教程。实际旧请求固定预算利用率为 0.814875，归档工作引用缩至 0；这不证明其是重复读取的唯一原因。现在按有效 schema 使用精简读取协议，完整工具指导和默认提示逐字保留，权限和预算不变。受控 SessionManager 请求在结构化及旧式路径先 2 failed、4 passed，修复后扩展稳定前缀与恢复检查；最终聚焦 63 passed（22.20 秒）。新增恢复夹具最初四项把正常经典会话 idle 错当 completed，已按既有生命周期校正并加强最终正文与无错误断言，不修改产品状态规则。后续完整回归及真实业务复验见下。
- 3894dfc 的 WSL 全量为 3 failed、2686 passed、67 skipped、5 deselected（236.19 秒）：记忆适配夹具的 no_reader 找不到满足条件的输入，原测试结果中并无夹具假设的 100 字虚构填充。bcd1586 只调整测试：输入字符预算设为 7000（不是生产 16,000 token 预算）、移除虚构填充，并断言含真实读取头的完整记忆超预算而保底内容可容纳；原状态、私有事实和工具配对断言保留，10 项通过。Native 3894dfc 全量 157 passed、1 failed、1 skipped（178.16 秒），原结构化历史三秒等待时状态为 cancelled；原失败保留。
- 699e91b 仅在同步历史操作内复用已验证祖先，不跨异步作用域。Native 旧版三次读取打开祖先 7 次，修改后为 4 次；新增用例核验每次重开叶子、pin 内拒绝硬链接、阻止删除或重命名并在释放后恢复，以及下次作用域重新验证。安全审查无发现。原三秒用例单次前后均通过：pytest 4.19 / 3.69 秒，完整 profile 6.394 / 5.727604 秒；发布分别 1.655637 / 1.687710 秒，CreateFile 583 / 177、checked Open 566 / 160，fsync 均 16。打开次数减少 406，但单次测量且前测受并行 WSL 全量负载影响，不能声称发布耗时改善。后测原始计数 JSON 的基线 HEAD 字段硬编码，实际源码以报告的 actual-head.txt 中 bcd1586 为准。
- bcd1586 Native 15 文件完整组合 176 passed、4 平台 skipped（177.49 秒），保留原 3、8、20 秒期限及新增安全断言，报告 native-final-bcd1586-20261116-2e614d52adc6442e8cd6f02690bbed75；其中 pin、原结构化用例和十项记忆用例另为 12 passed（17.33 秒）。WSL 全量 2689 passed、68 skipped、5 deselected（226.28 秒），报告 .cache/reports/final-context-history-full-20261116-4zsUrc；94 个 Core 源码 SHA 不变，真实 Bubblewrap/prlimit 探针通过。
- 新的真实模型运行 customer-effective-guidance-20261116-8e34d5982f 使用 bcd1586，保留 117b 模板、SOP、七条反馈、八份知识库、原 20 项检查及 medium-auto、16,000 tokens / 64,000 字符、90 秒模型 / 30 秒确认、smart/sandbox；仅新增被动请求指纹与指导字符数记录。服务节点 59 次模型调用、100 条工具回执，14 次 RAG 全部成功、发送两次；停止文件触发取消后 shutdown 抛出 InterruptedError，磁盘 Flow 状态 cancelled，没有正常结果，不能称为正常排空。离线审计原 20 项中 12 项满足、8 项不满足或未到达，业务未通过。准备阶段在 Native 根发现生成的 pytest 缓存，已保存在该次报告内并再次核验源码干净，当时尚无实际 API 请求。运行前独立审计冻结原 20 项检查；CF001 critical 可作为暂定可接受判断，但不能断言用户明确排除了其他方案，CF004 与 NO-KB 的人工历史须分别核对。
- b626ac6 将现有严格条件下的共享记忆精简投影用于结构化路径：只对本轮已成功交付的同版本记忆生效，保留私有及项目事实；首读、失败、新一轮、内容修改或读取权限撤销时不适用。新增七项用例。cb722e0 修复历史搜索计算 Future 被误加到待保存集合：继续等待 context_operations 排空，真正写入错误仍传播；四项受控线程屏障回归覆盖。
- cb722e0 WSL 全量 2700 passed、68 skipped、5 deselected（229.67 秒），报告 .cache/reports/structured-projection-shutdown-full-20261116-xJMtvE；94 个 Core 源码哈希未变，实际隔离探针退出码 0。Native 17 文件组合 187 passed、4 平台 skipped（221.06 秒），报告 .cache/reports/native-projection-shutdown-final-20261116-8258a6dd8be54a5c80d46a62e7bbad5e，保留原 3、8、20 秒期限。
- cb722e0 的 [CI 43811369880](https://github.com/vukencc/Channel-Agent/actions/runs/43811369880) 成功：Windows 1231 passed、67 skipped、3 warnings（212.44 秒），Linux 2697 passed、71 skipped、5 deselected（231.61 秒）；integration 任务跳过。与上面的本地平台和选择集不同，不能拼接统计。
- 真实模型案例 customer-structured-projection-20261116-b9a522a199 使用 cb722e0 和相同 117b 模板、输入与预算，829.844 秒完成五节点严格交接，原 20 项检查 20/20；读取 1、模拟运维 2、模拟发送 4、转人工 2、RAG 6，71 条工具回执配对，shutdown_finished=true 且无 harness 错误。真实 model.completed 确认 fit=true、refs=0。独立语义审查仍发现四项缺陷，整体业务未通过：
  - CF002 将仅用于开票资格的财务管理员误加入退款资格；第二次真实 RAG 已完整返回 REF02，退款条件仅为所有者。
  - CF001 将单租户多项目扩大为多租户，又把待核实的无替代方案写成事实；critical 可暂定接受，不要求降级。
  - NO-KB 对同一问题执行四次 RAG，超过首次加两次重试的上限；归档不重置累计次数。
  - CF004 人工处理由本次无记录推断历史未处置或其他渠道，并将未限定的详情增造为订单详情页。
  退款的七天、首次付款及首次购买、未开票、路径、材料和不保证结果均通过；NO-KB 如实表达资料缺口及 false，CF004 没有重复 AI 回复且 true，均通过。严格完成和原检查通过不能追认为语义验收通过。
- 1261cbe 仅修改新设计助手生成节点的事实与政策指导，既有模板逻辑不变；受控回归先 1 failed，修复后 31 passed。真实 xhigh 设计 customer-evidence-design-20261116-5ffdc383abec 用时 70.221 秒，25/25 严格检查和两轮真实往返通过；仅索引 1、3、4 的提示词追加，旧前缀及其他字段完全一致，模板 SHA256 为 433f9aedf3cd0333e1a027cd3c797dcec384a9458a7b1869fc9583877f7b9239。设计及往返未运行节点或工具。
- 真实案例 customer-evidence-run-20261116-a8ce26378fa8 在 Native 1261cbe 运行 1143.89 秒，服务节点 52 次模型调用、94 条回执后停滞，父进程通过原停止文件正常取消并排空。前三节点严格完成，服务 cancelled、人工 blocked；原 20 项检查 12 满足，读取 2、模拟运维 2、模拟发送 2、转人工 0、RAG 5，112 条工具回执配对。技术案例暂定 critical/unknown 与两条回复语义通过；退款检索四次超过首次加两次重试上限，尚未完成便切换 NO-KB（一次检索无命中），财务及人工无产物，不预判结果。
- 只读诊断 diagnosis-readonly-20261116T062910Z-6790dda8 核对 43 个归档块及 journal 精确一致，并非数据丢失。实测有历史读取能力时，最新未读工具批次仍可被硬归档且引用为零：例如第 12 次模型调用取得两份历史结果后，第 13 次实际请求仅四条固定消息、无工具结果；另一次历史和 RAG 结果后下一请求也无工具结果，随后反复检索自己的检索结果。
- 543fb0b 已提交并推送，Native 已同步：无论是否有历史读取工具，保护本轮所有未成功交付的完整批次（包括部分交付后），以实际成功窗口的精确位置、载荷及随机轮次 token 确认交付。审查以 AST 实证重复 ID 的 occurrence 误确认 P2，修复后复审无发现；31 项新交付用例及 17 项历史工具小页用例覆盖。历史工具完整 JSON 上限为 TOOL_MAX_OUTPUT 与 1500 中较小值，并提供真实分页、offset、遗漏或截断及读取提示。原文、工具配对、权限和 16,000 tokens / 64,000 字符预算不变。
- 旧 9674157 在新交付用例中有四项因果失败；四处旧夹具明确补充受控的已交付前提，预算、负载和原断言均不改。相关六文件 111 passed（3.2 秒），不能作为两组各 111 项相加；新文件一处读取显式使用 UTF-8 以兼容 Native GBK。这些是受控测试，非实际模型执行。
- 543fb0b 本机 Linux 首次全量 7 failed、2750 passed、68 skipped、5 deselected（233.36 秒），实际隔离探针退出码 0，94 个 Core 哈希未变；报告 .cache/reports/history-first-delivery-final-full-20261116-0d8c1c2a80024545b896ec17fc6e99fb。七项失败均来自两个旧夹具文件的旧交付语义：fresh_tool_context 四个参数只追加 assistant 消息就假设已读取，并期望 reader 归档未读批次；context_overhead 两个参数缺少多轮已读取前提。
- 5f22bad 仅适配上述两个测试文件：先前已读场景使用实际完整重试窗口的受控 ACK，不改预算、正文或原断言；旧归档场景使用真实本地发布夹具，要求有无 reader 均恢复完整未读配对，journal、chunks、revision 与原文不变。相关六文件 87 passed（2.55 秒），不能与失败全量拼成整组通过；Native 已同步且干净，生产 Core 仍为 543fb0b。
- Native 最终专项正在运行，报告 native-history-delivery-final-20261116-6eec9ceb3754：25 项选择、24 文件，默认 GBK，保留原五秒队列和两个原 20 秒退出用例，应用与测试目录并列且禁真实 provider。结果待验；为避免 CPU 干扰，Native 结束后再跑 Linux 完整回归。新真实客服案例仍在准备，尚无模型调用。
- 9674157 同步工作区 pin 的 [CI 43816698959](https://github.com/vukencc/Channel-Agent/actions/runs/43816698959) 和 [43816706479](https://github.com/vukencc/Channel-Agent/actions/runs/43816706479) 均成功，但 Native 原五秒用例在 1261cbe 前测和 9674157 后测均 3/3 失败，整个调用分别为 14.202 / 14.758 / 14.231 秒和 16.252 / 14.147 / 14.231 秒。单次 profile 的 get 为 37 次、3.13 秒，含项目读取 1.558 秒及 Flow 校验 1.187 秒；片段重叠且 profile 包装器额外 1.659 秒，不能推算生产耗时比例。
- 已推送并同步 Native 的 e0e5f8c 将同一作用域纯相对路径计算由 13 次降至不超过 2 次，安全检查不减少；WSL 22 passed、8 项 Native skipped，独立 14 passed 且审查无发现。Native 原五秒用例再次 3/3 失败，整个调用 16.360 / 16.225 / 15.972 秒，报告 flow-queue-path-calculation-20261116-a46a6b36eb4241af8b8b299374e8267a。本机问题仍未解决，CI 成功不能抵消，后续不盲目增加 pin 或削弱检查。
- 同一 1261cbe 的 [push CI 43814272657](https://github.com/vukencc/Channel-Agent/actions/runs/43814272657) 成功；[PR CI 43814277753](https://github.com/vukencc/Channel-Agent/actions/runs/43814277753) Linux 成功，Windows 原五秒队列测试超时，1 failed、1231 passed、67 skipped（214.06 秒）。
- 本机 WSL 首轮全量遗漏 nvm PATH，需 Linux Node 的断言失败，40 failed、2645 passed、84 skipped、5 deselected（306.89 秒），隔离探针退出码 0，报告 .cache/reports/evidence-designer-final-full-20261116-b2e8ea6d208c407da852dc3cc7d73a66。正确设置 PATH 后全量 2701 passed、68 skipped、5 deselected（231.66 秒），94 个 Core 源码哈希不变、隔离探针退出码 0，报告 .cache/reports/evidence-designer-node-full-20261116-3cdb0a8b5fed479eab6acfa7e02bcbae；首次失败保留。
- 主题匹配的实际五节点模板已完成 1280×800、1024×800 静态离屏检查：聊天角色、输入、底部模型和关闭/清空均可见且不重叠；全图适配后的节点字较小，1024 下尤甚。初始未套主题空画布截图保留，但不作为产品外观证据。用户禁止操作桌面和浏览器；这不是可见 GUI 或完整窗口操作验收。
- b8dc254 候选虽有 15 项自检，但缺少安全修复及后续改动，不是最终成品。[发布前构建 43803417504](https://github.com/vukencc/Channel-Agent/actions/runs/43803417504) 使用 1366b22，测试和 Windows、两个打包任务均成功，镜像及发布任务跳过；它早于 3894dfc 和历史目录 pin 改动，不能作为最终成品。

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

产品源码 `b8dc254` 已提交并推送：设计助手提示词默认按节点职责显式生成 `tool_names`，仅选所需基础工具及业务动态工具；用户明确要求继承时仍用 null。既有模板、用户工具配置、运行时、预算、推理及五节点四动态工具业务范围不变。最终集成审查无发现，128 项聚焦通过。本机 WSL 全量 2637 passed、64 skipped、5 deselected（232.11 秒），报告 `.cache/reports/explicit-tools-full-20261115-3sUBKo/`；[CI 43724156054](https://github.com/vukencc/Channel-Agent/actions/runs/43724156054) Windows 1156 passed、59 skipped（166.16 秒），Linux 2632 passed、69 skipped、5 deselected（215.82 秒）。

真实 xhigh 设计助手以 82.481 秒完成工具选择，只有五个节点的 `tool_names` 改变，其余字段逐值相等；实际导入导出两轮等价。各节点生效 schema 分别为 672、713、496、1490、732 估计 tokens，旧服务节点为 5849。所选模板 SHA256 `ef35dcefe814951aa669a6cc89219f4c6fdea4b2b24ed775bd9e81a68019cf3b`。报告为 Native `.cache/reports/customer-tool-selection-20261115-222721-bcad8678/`。

随后在原七条反馈、八份知识库及 gold、medium-auto、16,000 tokens / 64,000 字符和 Smart/Sandbox 下进行一次独立真实非 GUI 鲜跑，608.061 秒，首次五节点全部完成且严格交接均通过。四动态工具实际调用分别为读取反馈 1、提交运维 2、发送模拟回复 3、转人工 2，RAG 3 次。并行 Native 冻结构建，耗时不能作性能比较。CF004 保留失败步骤且未重复 AI 答复；NO-KB 未编造政策并转人工，运维明确为模拟，没有真实修复。

原业务检查 19 项满足、1 项不符：CF001 为 high 而 gold 为 critical，但原八份知识库及 README 没有分级 SOP。另经语义核对，退款回复遗漏所有者条件和完整申请路径，因此不能宣称业务全通过。此前各次失败保持独立证据，不能因本轮功能链完成而追认为通过。

SOP 与完整性输入变体保持原反馈、知识库、gold、预算、推理及五节点四动态工具。真实设计首轮 50.87 秒仅改服务节点但删除原交接说明 134 字，已拒绝且未导入；反馈后二次设计 53.823 秒完整保留原 690 字并增加 96 字通用条件清单，其他字段不变，JSON 往返等价。真实运行 420.956 秒，前三节点严格通过，服务节点在 16,915/16,000 估计 tokens 进入检查点、人工节点 blocked；退款所有者及完整申请路径经原文对照通过，新增 SOP 下 critical / unknown 正确，原 gold 检查 14 满足、6 不符或未到达，整体未通过。证据为 Native `.cache/reports/customer-sop-completeness-attempt2-20261115-225114-10f6cb59/`。

用户随后明确授权启用现有结构化历史，保留 16,000 tokens / 64,000 字符、原推理强度及会话私有原文按需读取，不提高预算。启用前安全审计发现 P2：Native 的 context junction 可越界读取，chunks junction 可使私有 RawHistory 先写入外部临时文件再最终拒绝；仅用临时合成数据真实复现。产品修复 `b017cad` 引入 history 目录 FD、Native 全链 pin、写前边界和块发布后核验，并适配 POSIX audit writer 的 directory_fd 及 DEBUG 适配器；owner、digest、audit、原子写和取消约束未降低。Native 已快进至该产品提交；WSL 后续 `4d01f9f` 只修 Windows audit 测试夹具与 CI，不改变产品源码。

Native 新文件红转绿后 7 passed、3 平台 skipped；加强写前 canary 在旧 `b8dc254` 失败、修复后通过。Flow 49 项及 ACL 21 项通过，history 22 passed、1 项 Windows 1314 skipped。rotation 旧 POSIX 假设在 Native 保留 1 failed，后续 UNC 定点 4 passed、0 skipped，不能拼成完整集通过；Native 最终组合正在复跑。Linux `b017cad` 全量 2651 passed、65 skipped、5 deselected（233.60 秒），运行期间最后修改了仅 Windows 测试夹具，不能作为最终源码全量；`4d01f9f` 最终全量正在 `.cache/reports/history-final-full-20261115-yuUujt/` 运行。真实隔离探针多次通过；最初安全复审 47 项聚焦无发现，后续发布后核验与可选读取器回退仍待最终复审。

结构化历史模板由真实 xhigh 设计助手用 52.839 秒生成，仅在五个节点原工具末尾加入 `history_search` / `history_read`，其他字段不变，两轮 JSON 往返等价；各节点 schema 分别为 1005、1046、829、1835、1065 估计 tokens。报告为 Native `.cache/reports/customer-structured-history-20261115-230946-9bea7ab1/`。使用原 SOP 输入、原模型 medium、Smart 与相同预算的独立鲜跑正在进行，没有人工 hard store 或重试；结果待定，不能提前记为通过。

`b8dc254` Windows 候选已完成 15/15 自检及独立审计，但不含上述 P2 修复，不是最终成品。最终代码须由正式标签流水线重新打包；0.6.1 尚未发布。

较早 `3ed5a28` 包的静态审计通过：ZIP 228 项 CRC、227 项清单大小/摘要与全部侧车一致，130 应用模块及 329 资源与对应提交一致；选定九个 PE 为 x64，运行库随包，无 CLR 或所查导入缺失。EXE SHA256 `110008f74056e1d62af9e8f020d815ae22bc2fa0b9e7649b7980e9febf84b340`；ZIP SHA256 `4589d0568a8c37a01efdce9fd0a4e27d4713fc88d698d030db8a9c02c1e580fe`。三项可编辑安装元数据含构建来源路径，未发现它们构成运行依赖；不等同于干净虚拟机兼容性或穷尽二进制敏感信息审计。该包仍非最终成品。

## 待完成

- 完成当前设计修订及后续真实模型鲜跑，逐项核对业务、节点状态和项目产物；保留所有此前失败，不把回归通过等同业务通过。
- 以最终源码构建并验证 Windows 成品，完成 PR 与发布前检查。用户禁止操作桌面及浏览器，可见界面操作验收仍未完成。
