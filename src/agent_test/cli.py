"""终端诊断展示：只读取有界元数据快照。"""


def render_debug(manager) -> list[tuple[str, str]]:
    """每次终端重绘读取有界快照；不会载入历史或显示工具正文。"""
    snapshot = manager.observer.snapshot()
    runtime = manager.concurrency_status()
    lines = [f'Agent {runtime["active_agents"]} · 模型 {runtime["active_model_calls"]} · '
             f'工具 {len(runtime["active_tools"])} · 活动模块 {len(snapshot["active"])} · 丢弃 {snapshot["dropped"]}']
    active = snapshot['active']
    activity_labels = {'reasoning': '推理中', 'content': '输出中', 'tool_arguments': '生成工具参数', 'status': '接收状态'}
    for row in active[-3:]:
        session = manager.sessions.get(row['session_id'])
        parent = session.record.get('delegated_from') if session is not None else None
        relation = f' ← {parent[:8]}' if parent else ''
        activity = activity_labels.get(row.get('activity'), '')
        lines.append(f'{row["session_id"][:8]}{relation} · {row["module"]}'
                     f'{" / " + row["name"] if row["name"] else ""}'
                     f'{" · " + activity if activity else ""} · {row["duration_ms"] / 1000:.2f}s')
    if not active:
        lines.append('当前无活动模块')
    elif len(active) > 3:
        lines[0] += f' · 另 {len(active) - 3} 个模块未展开'
    for row in snapshot['events'][-2:]:
        lines.append(f'最近 {row["session_id"][:8]} · {row["module"]} · {row["state"]}'
                     f'{" / " + row["error_type"] if row["error_type"] else ""} · {row["duration_ms"] / 1000:.2f}s')
    return [('class:status', '\n'.join(lines))]
