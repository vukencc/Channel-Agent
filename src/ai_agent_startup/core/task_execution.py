"""单个用户请求内的执行策略；不会创建用户输入或重置费用账本。"""
from dataclasses import dataclass
from itertools import count

from ai_agent_startup import config


@dataclass
class TaskExecutionPolicy:
    base_rounds: int
    automatic: bool
    continuations: int
    extendable: bool
    empty_replies: int = 0
    stage_tool_start: int = 0
    guidance: str = ''

    @classmethod
    def for_session(cls, record, base_rounds):
        automatic = base_rounds == 0 or config.AUTO_CONTINUE_TASKS
        extendable = (base_rounds > 0 and automatic and not record.get('delegated_from')
                      and 'MAX_TOOL_ROUNDS' not in record.get('budget_overrides', {}))
        return cls(base_rounds, automatic, config.TASK_MAX_CONTINUATIONS, extendable)

    @property
    def maximum_rounds(self) -> int | None:
        if self.base_rounds == 0:
            return None
        return self.base_rounds * (1 + self.continuations if self.extendable else 1)

    @property
    def progress_window(self) -> int:
        # 无上限模式仍定期检查执行进展；此窗口不会终止有进展的长任务。
        return self.base_rounds or 16

    def rounds(self):
        maximum = self.maximum_rounds
        return count() if maximum is None else range(maximum)

    def budget_guide(self, index):
        if self.maximum_rounds is None:
            return ('\n当前用户请求不设累计模型交互次数上限。'
                    '继续完成已授权且可执行的任务，核验产物和计划后再结束。'
                    '用户取消、必要信息缺失、工具持续无进展或显式费用预算耗尽时停止并如实说明。'
                    + self.guidance)
        if not self.automatic:
            return (f'\n本轮剩余模型交互次数：{self.base_rounds - index}。'
                    '预留最后一次核对结果并总结；接近上限时结束当前可运行阶段，如实说明未完成部分，不再启动新的大改写。')
        return (f'\n当前用户请求剩余模型交互上限：{self.maximum_rounds - index}；'
                f'每阶段 {self.base_rounds} 次，最多 {self.continuations if self.extendable else 0} 次自动续阶段。'
                '保存阶段进度不代表任务完成；继续执行未完成且可执行的计划步骤，核验后更新计划。'
                '没有证据不能声明完成；确需用户信息或达到安全预算时如实说明。'
                + self.guidance)


def execution_state(manager, session):
    """只依据已有计划/执行器状态，不把子代理文本当成控制指令。"""
    plan = manager.task_plans.peek(session) if manager.task_plans is not None else None
    unfinished = bool(plan and plan['status'] not in {'none', 'archived', 'completed'})
    active_child = False
    queue = manager.agent_tasks
    if queue is not None:
        for identifier, record in queue.records.items():
            if record['owner'] != session.id:
                continue
            runner = queue.tasks.get(identifier)
            child = manager.sessions.get(record['child_id'])
            if (record['status'] in {'queued', 'running'} or (runner and not runner.done())
                    or (child and (child.busy or child.tool_workers))):
                active_child = True
                break
    external_actionable = (manager.task_plans is not None
                           and manager.task_plans.execution_pending_or_reviewable(session))
    waiting = unfinished and not external_actionable and plan['status'] in {'blocked', 'waiting', 'failed', 'cancelled'} and not active_child
    return unfinished or active_child, waiting
