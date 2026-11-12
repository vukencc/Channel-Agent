# 工作区共享记忆验证记录（2026-11-12）

> 文档类型：本轮验证记录 · [使用指南](workspace-memory.md) · [文档导航](README.md)

本记录区分真实模型执行、受控回归和平台验证。记录时最终源码为 `9c9cd39`，Windows 与 WSL 源码同 HEAD；尚待完成的测试、成品和 CI 不计为通过。

## 真实模型与项目隔离

在隔离的合成“银杉客服”项目中使用真实 provider 执行两节点 Flow，再以新的状态目录启动新会话。全过程 60.743 秒，三次主模型调用和三次辅助提炼，三次 `last_run.workspace_memory.summary` 均为 `generated`。两个 Flow 节点均 completed，后节点报告 `has_shared_reference=true`；新会话读到此前项目事实，私有记忆为空。共享快照修订由 1 增至 2，再增至 3；Flow 结束后含 9 条项目事实、2 条进度及 5 条经验。该次真实执行使用初版实现 `8fd4540`；后续平台、取消及预算修订按以下专项和整组回归分别验证，不将早期结果追认为所有后续代码的鲜跑。

原用户项目、会话和知识库未改动；已有模型连接只在内存中复制读取，没有写入凭据。本地忽略目录中的证据为 `.cache/reports/workspace-memory-20261112-mxiy3hec/real-model-result.json`，不随文档分发。模型摘要仍是自报内容，节点完成不等于独立业务验收。

## 回归与并发

Linux 一轮非集成全量结果为 2236 passed、39 skipped、5 deselected（170.65 秒，linux-full2）；增加 Windows 句柄回归后的 linux-full3 为 2237 passed、42 skipped、5 deselected（167.07 秒）。随后辅助输入预算边界补丁的会话与模型专项 34 passed。新存储专项 17 passed，包含四个 spawn 进程的 12 条更新全部保留；NativeStore 专项 16 passed、1 skipped。真实 Bubblewrap/prlimit 隔离探针通过。

回归覆盖只读模式零共享写入且无辅助请求、Smart 限定元数据动作、历史权限模式确认、摘要不输入私有原文/思考/工具原文、已知密钥输入输出遮盖、不同工作区隔离、损坏 JSON、硬链接和根身份变化，以及普通和结构化窗口与恢复调用的读取预算。还覆盖辅助请求预算和未知价格在 HTTP 前拒绝、取消确认和不同保存阶段、错误重试收尾，以及更新失败或提交状态不确定时阻断下游。无关闲聊的摘要为 null 时跳过，不创建记录。

Linux 首轮全量为 3 failed、2227 passed、37 skipped、5 deselected（186.58 秒）：功能关闭时多保存一次 cancelled，以及保存期间 SSE 停止改变旧行为。随后修复生产行为，未修改原断言，原有 42 项聚焦检查通过。

## Windows 句柄与跨后端写入

原生 Windows 持有读取句柄时，最初复现 `WinError 5` 和 `WorkspaceMemoryCommitUncertain`，原快照 revision 1 保持不变；只增加 `SHARE_DELETE` 仍失败。随后仅对共享快照采用 `FileRenameInfoEx`（信息类 22，标志 `REPLACE_IF_EXISTS | POSIX_SEMANTICS`，值为 3）；普通工具仍使用信息类 3，祖先、链接、硬链接及只读属性保护未放松。相关结构见 [Microsoft 文档](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)。最终原生句柄专项 8 passed、1 skipped，覆盖默认读取句柄阻止替换、快照读取者看到旧 JSON、新读取者看到新 JSON、只读拒绝、不支持时失败和发布后 flush 不确定。

Windows 与 WSL 实际混合写入使用同步 barrier，各写 6 条，共 12 条，最终 revision 12、无丢失；写入区间重叠 0.761 秒。初次混合写入使用旧信息类 3 失败，修复后在新目录重新运行通过，不能将初次失败追认为通过。本机结果为 `D:\AI\AgentSrcCode\ChannelAgent\.cache\reports\workspace-memory-20261112-2doxax68\cross-backend-result.json`，不随文档分发。Windows 上 `.channelagent` 实际设置了 HIDDEN 属性。

## Windows 失败过程与待补验证

Windows 首轮未使用 CI 的 UTF-8 环境，结果为 31 failed、605 passed、60 skipped、39 errors（385.35 秒），包含 GBK 读取 fixture 和旧兼容问题；新 JSON 测试随后显式使用 UTF-8。GBK 第二次专项为 59 passed、4 skipped、1 failed（69.33 秒），失败为快照读取者使用旧 API；修复后第三次专项为 65 passed、3 skipped（53.63 秒）。

Windows 整组第三轮为 1 failed、686 passed、60 skipped（345.42 秒），唯一失败是新增辅助超时测试错误地把所有文件系统写盘也计入 1 秒。测试随后调整为启动等待 5 秒、辅助请求开始后仍在 1 秒内取消、另给磁盘排空 5 秒；生产辅助超时及原有 20/12 秒用例未更改。最终新功能在显式关闭 Python UTF-8 模式、使用本机 GBK 默认编码时为 66 passed、3 skipped（54.16 秒）。

Windows 第四轮整组为 1 failed、687 passed、60 skipped（344.71 秒），原生桌面子进程 20 秒内仅输出 `child start`，尚未完成导入。一次独立导入测量为 3.588 秒；有父 QApplication 的三项对照通过（20.49 秒），无父 QApplication 的两项通过（10.74 秒），子进程约 4.1–4.6 秒退出。没有复现原超时或支持父消息泵阻塞的证据，因此未修改退出用例、时限或产品导入，不将原失败追认为修复。

最新 Windows 整组、成品打包和 [PR #49](https://github.com/vukencc/Channel-Agent/pull/49) 的 CI 结果待补。此功能位于桌面、Web、CLI 和 headless 共用的 Core，无新增 UI 布局；本轮真实模型验证直接使用 Core 会话与 Flow 服务，未将受控界面或回放冒充真实模型运行。
