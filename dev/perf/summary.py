"""固定合成历史，测量真实摘要请求的前台等待与后台完成耗时。"""
import argparse
import asyncio
import json
import tempfile
import time
from functools import partial
from pathlib import Path


async def measure():
    from ai_agent_startup import config
    from ai_agent_startup.core.context import prepare_model_history
    from ai_agent_startup.core.sessions import SessionManager
    from ai_agent_startup.core.storage import SessionStore
    from ai_agent_startup.core.llm import complete, close_clients
    config.MODEL_INPUT_CHARS = 1800
    config.CONTEXT_SUMMARY = True
    config.MODEL_PARAMETERS = {'max_tokens': 256}
    messages = [{'role': 'system', 'content': '仅总结已知事实。'},
                {'role': 'user', 'content': '测试订单编号 A17，任务是整理待办项。'},
                {'role': 'assistant', 'content': '历史记录中的普通文本。' * 250},
                {'role': 'user', 'content': '继续整理。'}]
    with tempfile.TemporaryDirectory(prefix='agent-summary-') as directory:
        root = Path(directory)
        store = SessionStore(root / 'state', root / 'workspace')
        manager = SessionManager(store)
        session = manager.create()
        callback = (partial(manager._schedule_summary, session) if
                    getattr(config, 'CONTEXT_SUMMARY_BACKGROUND', False) else None)
        kwargs = {'schedule_summary': callback} if callback else {}
        try:
            start = time.perf_counter()
            _, metrics = await prepare_model_history(messages, judge=partial(complete, session_id=session.id),
                                                      cache=session.context_summaries, **kwargs)
            foreground = time.perf_counter() - start
            if getattr(session, 'summary_task', None):
                await session.summary_task
            completed = time.perf_counter() - start
            _, second = await prepare_model_history(messages, judge=partial(complete, session_id=session.id),
                                                    cache=session.context_summaries, **kwargs)
            return {'foreground_seconds': foreground, 'completed_seconds': completed,
                    'first_summary_status': metrics.get('summary'), 'second_summary_status': second.get('summary'),
                    'cache_entries': len(session.context_summaries),
                    'background': getattr(config, 'CONTEXT_SUMMARY_BACKGROUND', False),
                    'model': getattr(config, 'AUX_MODEL', '') or config.MODEL}
        finally:
            await manager.shutdown()
            await close_clients()
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(measure())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
