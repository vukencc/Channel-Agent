"""根据当轮实际工具与调用关系生成委派执行指导。"""


def delegation_guide(tool_names, delegated_from=None, *, concise=False) -> str:
    """仅向可创建并等待子任务的根 Agent 提供并行委派建议。"""
    names = set(tool_names or ())
    creator = 'create_session' if 'create_session' in names else 'delegate' if 'delegate' in names else None
    if delegated_from or creator is None or 'wait_agent_task' not in names:
        return ''
    if concise:
        return f'[协作] 独立任务可用 {creator}；wait_agent_task 等待并验收，勿重复委派。'
    return (
        '[子 Agent 协作] 简单耦合任务直接完成；复杂任务有独立探索、实现或验证时，'
        f'主动用 {creator} 委派两个独立子任务，自己同时推进。'
        '写清目标、必要上下文、文件所有权、验收与证据；工具只能来自父会话，沿用确认及权限。'
        '同一文件不得多方并发写入，子 Agent 禁止递归委派。'
        '保存 task_id，用 wait_agent_task 有界等待，超时不代表结束，勿反复轮询或重复创建。'
        'status 与 drained 均满足后核验产物及测试，再报告整体验收结果。'
    )
