# 缺陷修复清单与验证记录

每项先失败测试，再最小修复和独立提交；所有运行数据使用临时目录。

- [x] BUG-01
- [x] BUG-02
- [ ] BUG-03
- [ ] BUG-04
- [ ] BUG-05
- [ ] BUG-06
- [ ] BUG-07
- [ ] BUG-08
- [ ] BUG-09
- [ ] BUG-10
- [ ] BUG-11
- [ ] BUG-12
- [ ] BUG-13
- [ ] BUG-14
- [ ] BUG-15
- [ ] BUG-16
- [ ] BUG-17（P2：低风险实现或设计）
- [ ] BUG-18（P2：低风险实现或设计）
- [ ] BUG-19（P2：低风险实现或设计）
- [ ] BUG-20（P2：低风险实现或设计）

## BUG-01

根因是预算遗漏 schema 与无界记忆。core/context.py 统计 messages/schema/memory/extra；core/sessions.py 将预算不足转换为 checkpoint；core/storage.py 限制记忆追加与注入。新增 test_bug01_budget.py 三项回归，既有慢上下文测试仅适配参数签名，断言保持。配置 MEMORY_MAX_CHARS=4000。

验证：`uv run pytest dev/tests/test_bug01_budget.py dev/tests/test_context.py dev/tests/test_sessions.py -q（23 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。

## BUG-02

整轮持有信号量造成确认阻塞；core/sessions.py 改为仅模型调用占槽，评估独立池 ASSESS_CONCURRENCY=1。新增 test_bug02_slots.py 验证四个等待工具时第五会话可完成。

验证：`uv run pytest dev/tests/test_bug02_slots.py dev/tests/test_sessions.py -q（15 passed）`。新增回归先在旧实现失败，再通过；详见对应提交测试文件。
