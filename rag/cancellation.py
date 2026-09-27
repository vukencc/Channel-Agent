"""在可安全中断的批次和阶段边界检查工具取消标志。"""


def check_cancelled():
    # 延迟导入避免工具注册时形成循环依赖。
    from tools.sandbox import cancellation_requested
    if cancellation_requested():
        raise InterruptedError('检索已取消；尚未开始后续阶段')
