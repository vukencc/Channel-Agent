"""隔离 DEBUG 页面验收服务：受控模型回调、真实工具/子任务/通信，所有状态位于临时目录。"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile

from agent_core import config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--rounds', type=int, default=1, help='真实工具验收轮数，1–100；多轮用于密集概览验收')
    parser.add_argument('--children', type=int, default=2, help='真实子 Agent 数量，2–4；默认同时验证两个子 Agent')
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error('--rounds 必须在 1–100 之间')
    if not 2 <= args.children <= 4:
        parser.error('--children 必须在 2–4 之间')
    config.DEBUG = True
    config.DEBUG_TRACE_MAX_EVENTS = 10000 if args.rounds > 1 else 2048
    config.ENABLE_AGENT_TASKS = True
    config.ENABLE_SESSION_BUDGETS = True
    config.ENABLE_TASK_PLANS = False
    config.MEMORY_AUTO_EXTRACT = False
    config.RAG_ASSESS = False
    config.AUTO_CONTINUE_TASKS = False
    config.MAX_TOOL_ROUNDS = 4
    # 验收不继承本机监督器连接，避免多轮测试触发外部模型请求。
    os.environ['SUPERVISOR_ENABLED'] = 'false'

    import uvicorn
    from agent_core.core.context import estimate_tokens, request_tokens
    from agent_core.core.storage import SessionStore
    from agent_frontend.web.app import create_app

    with tempfile.TemporaryDirectory(prefix='agent-debug-timeline-') as temporary:
        root = Path(temporary)
        config.AUDIT_LOG = root / 'default-audit.jsonl'
        store = SessionStore(root / 'state', root / 'workspace')
        app = None

        async def controlled_model(history, **kwargs):
            # 这是 UI 验收回调，并非真实模型服务；用量只按实际输入输出估算。
            await asyncio.sleep(.18)
            child = app.state.manager.sessions[kwargs['session_id']].record.get('delegated_from')
            if history[-1]['role'] == 'tool':
                reply = {'role': 'assistant', 'content': '验收回调已收到真实工具结果；执行完成。'}
            elif child:
                reply = {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'child-command', 'function': {'name': 'run_command',
                    'arguments': json.dumps({'command': "sleep 0.25; python3 -I -c 'print(sum(range(50000)))'",
                                             'reason': '临时沙箱真实命令验收'})}}]}
            else:
                reply = {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'read-one', 'function': {'name': 'read_file', 'arguments': '{"path":"one.txt"}'}},
                    {'id': 'read-two', 'function': {'name': 'read_file', 'arguments': '{"path":"two.txt"}'}}]}
            kwargs['emit']('metrics', {'input_tokens': request_tokens(history),
                'output_tokens': estimate_tokens(json.dumps(reply, ensure_ascii=False)), 'tokens_source': 'estimate'})
            return reply

        app = create_app(store=store, model=controlled_model)
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def validation_lifespan(application):
            async with original_lifespan(application):
                manager = application.state.manager
                manager.confirmation_handler = lambda *_: True
                parent = manager.create(title='DEBUG 隔离验收')
                folder = manager.workspace(parent)
                (folder / 'one.txt').write_text('用于真实文件工具的第一份临时资料', encoding='utf-8')
                (folder / 'two.txt').write_text('用于真实文件工具的第二份临时资料', encoding='utf-8')
                manager.submit(parent, '读取并核验两份临时资料')
                task_ids = []
                child_ids = []
                for child_index in range(args.children):
                    task_id = await manager.agent_tasks.start(parent, f'子 Agent {child_index + 1} 执行一个真实沙箱命令', ['run_command'], 2)
                    task_ids.append(task_id)
                    child_id = manager.agent_tasks.records[task_id]['child_id']
                    child_ids.append(child_id)
                    manager.communication.send(parent.id, child_id, '请完成命令后报告结果')
                await asyncio.gather(parent.task, *(manager.agent_tasks.tasks[task_id] for task_id in task_ids))
                for child_id in child_ids:
                    manager.communication.send(child_id, parent.id, '真实命令已经结束')
                manager.communication.read(parent.id)
                for index in range(1, args.rounds):
                    # 每轮均经过真实调度、工具与邮箱；受控模型不产生服务费用。
                    manager.submit(parent, f'第 {index + 1} 轮读取两份临时资料')
                    for child_id in child_ids:
                        manager.communication.send(parent.id, child_id, f'验收第 {index + 1} 轮')
                        manager.submit(manager.sessions[child_id], f'第 {index + 1} 轮执行真实沙箱命令')
                    await asyncio.gather(parent.task, *(manager.sessions[child_id].task for child_id in child_ids))
                    for child_id in child_ids:
                        manager.communication.send(child_id, parent.id, f'第 {index + 1} 轮已结束')
                    manager.communication.read(parent.id)
                await manager.flush()
                print(json.dumps({'validation': 'controlled_model_real_tools', 'url': f'http://127.0.0.1:{args.port}/debug',
                                  'parent_id': parent.id, 'child_ids': child_ids, 'rounds': args.rounds, 'state_directory': str(root)}, ensure_ascii=False), flush=True)
                yield

        app.router.lifespan_context = validation_lifespan
        try:
            uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)
        finally:
            store.close()


if __name__ == '__main__':
    main()
