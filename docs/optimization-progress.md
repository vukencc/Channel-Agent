# 优化实施记录

按 P0 → P1 → P2 顺序执行；每项先基准或失败测试，再实现并独立提交。
用户数据目录保持不变，所有测量输出使用 `/tmp/agent-perf-results/`。
初始工作树只有用户未跟踪的报告与任务提示，不覆盖其问题描述。

## 任务清单

- [x] P0：PERF-05 会话懒加载
- [ ] P0：PERF-06 上下文单次序列化
- [ ] P0：PERF-07 配额节流
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
