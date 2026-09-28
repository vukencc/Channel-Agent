# 优化实施记录

按 P0 → P1 → P2 顺序执行；每项先基准或失败测试，再实现并独立提交。
用户数据目录保持不变，所有测量输出使用 `/tmp/agent-perf-results/`。
初始工作树只有用户未跟踪的报告与任务提示，不覆盖其问题描述。

## 任务清单

- [x] P0：PERF-05 会话懒加载
- [x] P0：PERF-06 上下文单次序列化
- [x] P0：PERF-07 配额节流
- [ ] P0：PERF-08 审计句柄
- [ ] P0：PERF-12 流式导出
- [ ] P0：FREE-03 文件工具
- [ ] P0：FREE-05 模型参数
- [ ] P0：FREE-13 成本与速率预算
- [ ] P0：FREE-16 提示词模板
- [ ] P1：PERF-01 重排与缓存
- [ ] P1：PERF-02 可选 ANN
- [ ] P1：PERF-03 增量持久化 BM25
- [ ] P1：PERF-04 内存上限
- [ ] P1：PERF-09 推理并发隔离
- [ ] P1：PERF-10 稳定前缀与工具子集
- [ ] P1：PERF-11 辅助模型
- [ ] P1：FREE-01 多工具根
- [ ] P1：FREE-04 权限策略
- [ ] P1：FREE-07 多知识库与过滤
- [ ] P1：FREE-08 记忆管理
- [ ] P1：FREE-09 headless
- [ ] P1：FREE-10 分支与重发
- [ ] P2：FREE-02 网络与长任务设计、实现
- [ ] P2：FREE-06 会话预算设计、实现
- [ ] P2：FREE-11 后台任务与子代理设计、实现
- [ ] P2：FREE-12 多模态设计、实现
- [ ] P2：FREE-14 可观测命令设计、实现
- [ ] P2：FREE-15 跨平台设计、实现

PERF-13/14 未列入本次阶段目标，暂不变更渲染与保存持久性策略。

## PERF-05

管理器与 `--list` 只加载元数据，读取消息或修改会话时才校验历史并恢复中断工具。
兼容 v1；旧版单文件必须解析，首次保存继续使用原迁移逻辑。未启用历史自动卸载或删除。
没有新增配置或依赖。`--list` 不再恢复/写入会话；切换或提交时恢复。

测量：`uv run python -m dev.perf.benchmark sessions --output /tmp/agent-perf-results/perf05-{before,after}.json`。
两个独立进程，各生成 100 个约 10 MiB 的 v2 会话，准备数据不计时；只测管理器构造。
耗时 **1.969569 → 0.003993 秒**，进程峰值 RSS 增量 **1016.246 → 0 MiB**（ru_maxrss，非绝对内存）。
首次查看目标会话仍需加载其消息；不声称完整 TUI 冷启动为 4 ms。

失败测试先观察到启动读取 3 份历史；修复后仅首次访问目标读取一份。
针对性回归：`uv run pytest dev/tests/test_perf05_lazy_sessions.py dev/tests/test_sessions.py dev/tests/test_cli_performance.py -q`。
环境说明：受限执行中异步唤醒停滞；授权在限制外运行同一测试通过，未改测试或安全行为。

## PERF-06

调用内保存每条消息的序列化/字符/token 计量；修改的工具参数单独复制，其他嵌套内容只读共享。
ASCII 计数使用标准库 C 编码实现；摘要输入在预算处停止序列化，不构造全量淘汰历史。
10,001 条混合中英文消息、3 次采样中位 **0.732597 → 0.053915 秒（下降 92.64%）**。
前后发送 27,810 字符、估算 15,426 tokens、淘汰 4,986 轮，完全一致。
命令：`uv run python -m dev.perf.benchmark context --output /tmp/agent-perf-results/perf06-after.json`。
验证：`uv run pytest dev/tests/test_context.py dev/tests/test_perf06_context.py dev/tests/test_bug08_summary.py dev/tests/test_sessions.py -q`。
新增测试先确认未修改消息序列化 9 次，优化后 1 次；原始记录不可变测试保持通过。
无新增配置/依赖；无模型调用，token 为本地估算，不是服务商账单。

## PERF-07

命令配额扫描按单调时钟节流，输出到达不再重复触发全目录遍历；开始、确认后、结束检查保留。
`COMMAND_QUOTA_INTERVAL=0.1` 保留既有采样频率；显式改 1 秒可进一步降 IO，但会扩大超额发现窗口。
单文件 rlimit、超额终止及审计均保留。配额仍是采样检测，不是文件系统硬配额。
真实 Bubblewrap 基准：3,000 个文件，100 次 8 KiB 输出，间隔 5 ms。
墙钟 **1.776154 → 0.582655 秒**；父进程 CPU **1.775906 → 0.132174 秒**；扫描 **104 → 7 次**，
扫描耗时 **1.771452 → 0.127973 秒**。默认参数下测量，无主机执行回退。
验证：`uv run pytest dev/tests/test_perf07_quota.py dev/tests/test_command.py dev/tests/test_bug04_limits.py -q`：26 passed。
测量命令：`uv run python -m dev.perf.benchmark quota --output /tmp/agent-perf-results/perf07-after.json`。
