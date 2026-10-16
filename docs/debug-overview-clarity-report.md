> 历史报告，不作为当前使用规范；保留当时的问题描述和实测结果。当前说明见[当前调试与上下文指南](debug-runtime.md)。

# DEBUG 总览清晰度交付报告

## 根因与修复

旧版总览为每个子 Agent 单独绘制跨行的分叉、汇流线，并直接叠加全局消息连线。多个子 Agent 同时运行时，这些长线穿过兄弟支线和事件标签，导致整体过程和异常难以快速辨认。本轮改为根 Agent 行为、事件与异常总览；子 Agent 默认紧凑显示，按需展开。共享 fork/join 母线只说明父子结构，不表示耗时；子 Agent 只画自身行内连接段。默认总览不画 message-edge，通信入口显示逐条发送、读取及未读 pending 状态。异常摘要分别计工具错误、拒绝及其它异常，Compact 单独显示，并可回到原始记录。

## 验证结果

使用既有真实 trace 的 4460 条事件、3 个 Agent 回放，旧总览高度为 672 px，新总览为 426 px；展开一个 child 后为 578 px。总览全局 message-edge 为 0，共享 fork/join 母线各 1 条，没有跨兄弟连线。该真实记录包含 5 个工具错误和 1 个拒绝，摘要为 6 个去重事故，原始异常记录 18 条，Compact 为 0；6 条通信中，2 条在收件 Agent 结束后发送，仍显示未读。此次没有发起新模型请求。

另以 8 个 child 的受控 UI fixture 检查布局：8 条独立行、0 个母线穿越工作区；4 个已完成 child 显示 join，4 个活跃 child 保持开放。移动端 450 px viewport 的文档宽度为 435 px，无外溢；时间线可横向滚动并缩放至 2460 px，局部通信详情无外溢。此 fixture 不是模型执行。

6 个新增红测先复现问题，修复后 48 项 focused tests 通过。完整非集成测试为 `uv run pytest -m 'not integration' -q`：1235 passed、2 skipped、5 deselected，用时 32.59 秒；在 WSL Linux PATH 下以 `UV_NO_SYNC=1` 执行。最终 review 无发现，未改依赖或配置。截图与结果数据：[总览](../.cache/reports/debug-clarity-20261016-b84e14/overview.png)、[通信详情](../.cache/reports/debug-clarity-20261016-b84e14/messages.png)、[8 个 Agent 受控 fixture](../.cache/reports/debug-clarity-20261016-b84e14/eight-agents-controlled.png)、[移动视口](../.cache/reports/debug-clarity-20261016-b84e14/mobile.png)、[验证 JSON](../.cache/reports/debug-clarity-20261016-b84e14/validation.json)。此前真实模型实验记录未删除，本轮也未重做。
