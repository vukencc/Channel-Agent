"""已配置服务的真实 usage；只发送固定合成对话，不读取用户会话或调用工具。"""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path


async def measure(names=None):
    import config
    from core import llm
    from core.sessions import SessionManager
    from core.storage import SessionStore
    from core.prompts import DEFAULT_PROMPT
    config.MODEL_TOOL_NAMES = names
    config.MODEL_PARAMETERS = {'max_tokens': 64, 'tool_choice': 'auto'}
    config.MODEL_STREAM_USAGE = True
    config.RAG_ASSESS = False
    config.MEMORY_AUTO_EXTRACT = False
    config.MAX_RETRIES = 1
    config.MODEL_FALLBACKS = []
    metrics = []
    async def model(history, *, session_id, emit):
        def collect(kind, value):
            if kind == 'metrics':
                metrics.append(value)
            emit(kind, value)
        reply = await llm.call_model(history, session_id=session_id, emit=collect)
        if reply.get('tool_calls'):
            raise RuntimeError('基准只允许文本回答；拒绝执行模型提出的工具调用')
        return reply
    with tempfile.TemporaryDirectory(prefix='agent-prefix-') as directory:
        root = Path(directory)
        store = SessionStore(root / 'state', root / 'workspace')
        manager = SessionManager(store, model=model)
        try:
            session = manager.create(prompt=DEFAULT_PROMPT)
            states = []
            for prompt in ('请只回复“收到”，不要调用工具。', '请只回复“继续”，不要调用工具。'):
                manager.submit(session, prompt)
                await session.task
                states.append({'status': session.record['status'],
                               'error_type': session.record.get('error', '').split(':')[0][:80],
                               'context': session.record.get('last_run', {}).get('context')})
            return {'tool_names': names, 'stable_prefix': getattr(config, 'MODEL_STABLE_PREFIX', False),
                    'states': states, 'metrics': metrics,
                    'provider_usage_available': bool(metrics) and all(row['tokens_source'] == 'provider' for row in metrics)}
        finally:
            await manager.shutdown()
            await llm.close_clients()
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool-names', help='逗号分隔的工具名，未指定使用全部工具')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(measure(args.tool_names.split(',') if args.tool_names else None))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
