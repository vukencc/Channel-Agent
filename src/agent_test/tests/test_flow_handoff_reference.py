"""真实 Flow/SessionManager，受控离线模型；不代表真实业务验收。"""
import asyncio
import copy
import json

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.mark.parametrize('structured', [False, True])
def test_flow_tags_only_upstream_span_and_first_model_sees_full_input(tmp_path, monkeypatch, sandbox_env, structured):
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    project = tmp_path / 'external-project'
    project.mkdir()
    store = SessionStore(tmp_path / 'state', sandbox_env)
    seen, submitted = [], []
    output = '上游原始证据🙂独立内容'

    async def model(history, **kwargs):
        seen.append(copy.deepcopy(history))
        answer = output if len(seen) == 1 else '下游完成'
        return {'role': 'assistant', 'content': json.dumps({'evidence': answer}, ensure_ascii=False) if structured else answer}

    async def run():
        manager = SessionManager(store, model=model)
        original = manager.submit

        def submit(session, text, **kwargs):
            submitted.append((text, copy.deepcopy(kwargs)))
            return original(session, text, **kwargs)

        monkeypatch.setattr(manager, 'submit', submit)
        try:
            service = FlowService(manager)
            flow = service.create(workspace_path=str(project), handoff='用户背景原样保留')
            flow['nodes'] = [{'id': 'source', 'title': '源', 'prompt': '提供上游证据'},
                             {'id': 'target', 'title': '目标', 'prompt': '当前任务原样保留'}]
            if structured:
                for node in flow['nodes']:
                    node['handoff_contract'] = {'fields': {'evidence': {'type': 'string'}}}
            flow['edges'] = [{'source': 'source', 'target': 'target'}]
            flow = service.save(flow)
            service.start(flow['id'], input_text='本轮新增信息原样保留')
            await asyncio.wait_for(service.tasks[flow['id']], 8)
            assert service.get(flow['id'])['run']['status'] == 'completed'
            text, kwargs = submitted[1]
            start, end = kwargs['handoff_span']
            selected = text[start:end]
            assert output in selected and '交接' in selected
            assert '用户背景原样保留' not in selected
            assert '本轮新增信息原样保留' not in selected
            assert '当前节点任务' not in selected
            assert text in [m.get('content') for m in seen[1]]
            assert 'handoff_span' not in submitted[0][1] or submitted[0][1]['handoff_span'] is None
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()

