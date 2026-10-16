"""真实监督边界实验：合成历史是测试输入，不冒充执行结果或项目验收。"""
import asyncio
import json
from pathlib import Path
import tempfile
import time


async def main():
    from ai_agent_startup import config
    from ai_agent_startup.core.sessions import SessionManager
    from ai_agent_startup.core.storage import SessionStore

    root = Path(tempfile.mkdtemp(prefix='agent-project-validation-context-'))
    config.ENABLE_STRUCTURED_CONTEXT = True
    config.ENABLE_TASK_PLANS = False
    config.ENABLE_AGENT_TASKS = False
    config.ENABLE_COMMAND_JOBS = False
    config.CONTEXT_HISTORY_CHUNK_CHARS = 100000
    config.CONTEXT_COMPACT_RATIO = .9
    config.CONTEXT_SUMMARY_CHARS = 512
    config.CONTEXT_RESERVE_USER_CHARS = 0
    config.CONTEXT_RESERVE_SEARCH_CHARS = 0
    config.CONTEXT_RESERVE_USER_TOKENS = 0
    config.CONTEXT_RESERVE_SEARCH_TOKENS = 0
    config.MODEL_INPUT_CHARS = 200000
    config.MODEL_INPUT_TOKENS = 100000
    store = SessionStore(root / 'state', root / 'work')

    async def no_execution(*args, **kwargs):
        raise AssertionError('本实验只调用真实监督模型，不运行执行 Agent')

    manager = SessionManager(store, model=no_execution)
    service = manager.history_context
    delegate = service.supervisor
    delegate.settings = delegate.settings.model_copy(update={'check_interval': 1})
    decisions = []

    class Observed:
        settings = delegate.settings

        async def inspect(self, snapshot):
            started = time.monotonic()
            result = await delegate.inspect(snapshot)
            decisions.append({'units': len(snapshot['chunks']), 'values': result['values'],
                              'elapsed_s': round(time.monotonic() - started, 3)})
            return result

        async def summarize(self, previous, messages, *, kind='session'):
            return await delegate.summarize(previous, messages, kind=kind)

        async def close(self):
            await delegate.close()

    service.supervisor = Observed()
    cases = {}
    try:
        for name, unrelated, count, high in [('low_mixed_value', True, 8, False),
                                            ('high_relevant_only', False, 8, True),
                                            ('high_mixed_value', True, 8, True),
                                            ('high_protected_only', True, 2, True)]:
            session = manager.create(title=name)
            messages = [{'role': 'system', 'content': '仅回答用户当前任务，以下历史是测试输入，无工具执行。'}]
            for index in range(count):
                text = (f'历史话题{index}：海边度假、天气、猫咪命名，与软件项目无关。'
                        if unrelated and index % 2 else
                        f'WorkBoard需求{index}：SQLite依赖环检测、导入失败原子回滚、评论标签持久化，CLI输出JSON。')
                messages.append({'role': 'user', 'content': text + (' 这是显式的边界填充测试文本。' * 120 if high else '')})
            messages.append({'role': 'user', 'content': '继续完成WorkBoard的SQLite依赖和事务、CLI及导入导出测试。'})
            config.MODEL_INPUT_CHARS = 200000
            if high:
                from ai_agent_startup.core.history_context import _groups
                manifest = await service._manifest(session)
                available = _groups(messages)[:-1]
                keep = min(config.CONTEXT_HISTORY_KEEP_GROUPS, len(available))
                protected = available[-keep:] if keep else []
                previews = service._chunks(session, messages, available[:-keep] if keep else available)
                initial = service._compose(messages, manifest, previews, protected, len(messages) - 1, '', '')
                size = service._metrics(initial, [], messages[-1], messages)['total_chars']
                config.MODEL_INPUT_CHARS = int(size / .92)
            started = time.monotonic()
            _, metrics = await service.prepare(session, messages, schemas=[])
            cases[name] = {'input_char_budget': config.MODEL_INPUT_CHARS, 'metrics': metrics, 'wall_s': round(time.monotonic() - started, 3)}
        result = {'root': str(root), 'input_kind': 'synthetic history, no claimed execution',
                  'cases': cases, 'decisions': decisions}
        (root / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await manager.shutdown()
        store.close()


if __name__ == '__main__':
    asyncio.run(main())
