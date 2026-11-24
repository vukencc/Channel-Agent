"""真实 Flow/文件工具路径的受控离线模型回归；不调用外网模型或回放记录。"""
import asyncio
import copy
import json

import pytest

from agent_core import config
from agent_core.core.flow_progress_review import REVIEW_INSTRUCTION
from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import active_tool_names, active_tool_schemas


def _answer(content):
    return {'role': 'assistant', 'content': content}


def _tool(identifier, name, **arguments):
    return {'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': identifier, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)},
    }]}


@pytest.fixture
def offline_flow(tmp_path, monkeypatch, sandbox_env):
    for name in ('RAG_ASSESS', 'MEMORY_AUTO_EXTRACT', 'WORKSPACE_MEMORY_ENABLED',
                 'ENABLE_STRUCTURED_CONTEXT', 'AUTO_CONTINUE_TASKS'):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 16)
    monkeypatch.setattr(config, 'MODEL_TOOL_NAMES', ['create_file', 'read_file', 'list_files'])
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'private')
    yield store, sandbox_env
    store.close()


class OfflineModel:
    """只替换服务端模型响应；保留真实 schema 绑定、工具执行和落盘。"""

    def __init__(self, review_answer, *, pages=False):
        self.review_answer = review_answer
        self.pages = pages
        self.calls = []
        self.source = None
        self.round = 0
        self.review_started = asyncio.Event()
        self.pause_review = False
        self.timeout_review = False
        self.after_continue = False
        self.continue_tool = False

    async def __call__(self, history, *, session_id, emit):
        names = copy.deepcopy(active_tool_names.get())
        schemas = copy.deepcopy(active_tool_schemas.get())
        self.calls.append((session_id, copy.deepcopy(history), names, schemas))
        if self.source is None:
            self.source = session_id
        if session_id != self.source:
            return _answer('下游已接收')
        self.round += 1
        reads = 6 if self.pages else 3
        if self.round == 1:
            return _tool('write', 'create_file', path='result.txt', content='页甲\n页乙\n页丙\n页丁\n')
        if self.round <= reads + 1:
            offset = (self.round - 2) * 2 if self.pages else 0
            return _tool(f'read-{self.round}', 'read_file', path='result.txt', offset=offset, limit=4)
        if not self.after_continue:
            # 此断言在旧实现上直接失败，不能靠替换候选检测器触发。
            assert names == [] and schemas == []
            assert history[-1]['content'] == REVIEW_INSTRUCTION
            self.review_started.set()
            if self.pause_review:
                await asyncio.Event().wait()
            if self.timeout_review:
                self.after_continue = True
                raise TimeoutError('受控离线模型读取超时')
            self.after_continue = True
            return self.review_answer
        assert names and schemas
        assert all(message.get('content') != REVIEW_INSTRUCTION for message in history)
        if self.continue_tool:
            self.continue_tool = False
            return _tool('continue-read', 'read_file', path='result.txt', offset=8, limit=4)
        return _answer('补证完成')


async def _start(store, workspace, model, *, policy='full_access', contract=None):
    manager = SessionManager(store, model=model)
    service = FlowService(manager)
    flow = service.create(workspace_path=str(workspace), permission_policy=policy)
    flow['nodes'] = [
        {'id': 'source', 'title': '产物核对', 'prompt': '写入并核对小产物后交接',
         'tool_names': ['create_file', 'read_file', 'list_files'], 'handoff_contract': contract},
        {'id': 'target', 'title': '接收', 'prompt': '接收上游业务对象'},
    ]
    flow['edges'] = [{'source': 'source', 'target': 'target'}]
    flow = service.save(flow)
    service.start(flow['id'], input_text='核对本次临时产物')
    return manager, service, flow['id']


def _source(manager, run):
    return manager.sessions[run['nodes']['source']['session_id']]


def _assert_receipts(session, reads):
    messages = session.record['messages']
    results = [m for m in messages if m.get('role') == 'tool' and m.get('_provenance') == 'executed']
    events = [e for e in session.record['last_run']['tools'] if e['source'] == 'executed']
    assert len(results) == len(events) == reads + 1
    assert all(event['source'] == 'executed' and event['outcome'] == 'completed' for event in events)
    for result, event in zip(results, events):
        assert result['_event_id'] == event['event_id']
        assert result['tool_call_id'] == event['tool_call_id']
        assert result['content']


@pytest.mark.parametrize('pages', [False, True])
def test_review_handoff_keeps_business_contract_and_reaches_downstream(offline_flow, pages):
    store, workspace = offline_flow
    business = {'state': 'ok', 'artifact': 'result.txt'}
    model = OfflineModel(_answer(json.dumps({'decision': 'handoff', 'output': business})), pages=pages)

    async def run():
        manager, service, flow_id = await _start(store, workspace, model, contract={
            'fields': {'state': {'type': 'string', 'enum': ['ok']}, 'artifact': {'type': 'string'}}})
        try:
            await asyncio.wait_for(service.tasks[flow_id], 8)
            result = service.get(flow_id)['run']
            source_calls = [call for call in model.calls if call[0] == model.source]
            assert source_calls[-1][2:] == ([], [])
            assert result['status'] == 'completed', result
            assert result['nodes']['source']['handoff_validation'] == {'status': 'passed'}
            output = result['nodes']['source']['output_handoff']
            assert json.loads(output) == business
            downstream = [history for sid, history, _, _ in model.calls if sid != model.source]
            assert len(downstream) == 1
            assert output in result['nodes']['target']['received_handoff']
            assert any(output in (m.get('content') or '') for m in downstream[0])
            session = _source(manager, result)
            _assert_receipts(session, 6 if pages else 3)
            assert session.record['last_run']['flow_progress_reviews'][-1]['status'] == 'handoff'
            assert (workspace / 'result.txt').read_text() == '页甲\n页乙\n页丙\n页丁\n'
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('automatic', [False, True])
def test_continue_restores_tools_without_reviewing_the_same_stall(offline_flow, monkeypatch, automatic):
    store, workspace = offline_flow
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', automatic)
    goal = '补查 result.txt 的末页后再交接'
    model = OfflineModel(_answer(json.dumps({'decision': 'continue', 'output': goal})))
    model.continue_tool = True

    async def run():
        manager, service, flow_id = await _start(store, workspace, model)
        try:
            await asyncio.wait_for(service.tasks[flow_id], 8)
            result = service.get(flow_id)['run']
            assert result['status'] == 'completed', result
            source = _source(manager, result)
            reviews = source.record['last_run']['flow_progress_reviews']
            assert len(reviews) == 1 and reviews[0]['status'] == 'continue'
            assert reviews[0]['next_step'] == goal
            following = [h for sid, h, names, _ in model.calls[5:] if sid == model.source]
            assert len(following) == 2 and any(goal in (m.get('content') or '') for m in following[0])
            for history in following:
                assert any(m.get('role') == 'assistant' and m.get('content') == goal for m in history)
                assert all(goal not in (m.get('content') or '') for m in history if m.get('role') == 'system')
            _assert_receipts(source, 4)
            continued = next(m for m in source.record['messages'] if m.get('tool_call_id') == 'continue-read')
            assert 'offset=8' in continued['content'] and continued['_provenance'] == 'executed'
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('answer', [
    _answer('不是有效判断'),
    {**_answer('{"decision":"handoff","output":"done"}'),
     'tool_calls': _tool('forbidden', 'create_file', path='forbidden.txt', content='不得执行')['tool_calls']},
])
def test_invalid_review_blocks_downstream_and_never_executes_smuggled_tool(offline_flow, answer):
    store, workspace = offline_flow
    model = OfflineModel(answer)

    async def run():
        manager, service, flow_id = await _start(store, workspace, model)
        try:
            await asyncio.wait_for(service.tasks[flow_id], 8)
            result = service.get(flow_id)['run']
            source = _source(manager, result)
            assert source.record['status'] == 'checkpoint'
            assert source.record['last_run']['flow_progress_reviews'][-1]['status'] == 'invalid'
            assert result['nodes']['target']['status'] == 'blocked'
            assert all(sid == model.source for sid, *_ in model.calls)
            assert not (workspace / 'forbidden.txt').exists()
            _assert_receipts(source, 3)
            if answer.get('tool_calls'):
                rejected = next(m for m in source.record['messages'] if m.get('tool_call_id') == 'forbidden')
                assert rejected['_provenance'] == 'not_executed'
                event = next(e for e in source.record['last_run']['tools'] if e['tool_call_id'] == 'forbidden')
                assert event['source'] == 'not_executed' and event['event_id'] == rejected['_event_id']
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_optional_review_does_not_displace_normal_request_or_receipts(offline_flow, monkeypatch):
    from agent_core.core import flow_progress_review

    store, workspace = offline_flow
    # 判断提示不能容纳时，原可发出的普通窗口仍须完整发送。
    monkeypatch.setattr(flow_progress_review, 'REVIEW_INSTRUCTION', REVIEW_INSTRUCTION * 1000)
    model = OfflineModel(_answer('unused'))
    model.after_continue = True

    async def run():
        manager, service, flow_id = await _start(store, workspace, model)
        try:
            await asyncio.wait_for(service.tasks[flow_id], 8)
            result = service.get(flow_id)['run']
            assert result['status'] == 'completed', result
            source = _source(manager, result)
            assert not source.record['last_run'].get('flow_progress_reviews')
            _assert_receipts(source, 3)
            history = [h for sid, h, _, _ in model.calls if sid == model.source][-1]
            delivered = [m for m in history if m.get('role') == 'tool']
            original = [m for m in source.record['messages'] if m.get('role') == 'tool']
            assert len(delivered) == len(original) == 4
            assert [(m['tool_call_id'], m['content']) for m in delivered] == [
                (m['tool_call_id'], m['content']) for m in original]
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_readonly_denied_work_cannot_trigger_success_review(offline_flow):
    store, workspace = offline_flow
    (workspace / 'result.txt').write_text('只读原文', encoding='utf-8')
    model = OfflineModel(_answer('unused'))
    model.after_continue = True

    async def run():
        manager, service, flow_id = await _start(store, workspace, model, policy='readonly')
        try:
            await asyncio.wait_for(service.tasks[flow_id], 8)
            result = service.get(flow_id)['run']
            assert result['status'] == 'completed', result
            source = _source(manager, result)
            assert source.record['permission_policy'] == 'readonly'
            assert not source.record['last_run'].get('flow_progress_reviews')
            events = source.record['last_run']['tools']
            assert events[0]['outcome'] != 'completed'
            assert all(event['outcome'] == 'completed' for event in events[1:])
            assert (workspace / 'result.txt').read_text() == '只读原文'
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['cancel', 'timeout'])
def test_review_cancellation_and_timeout_preserve_receipts(offline_flow, mode):
    store, workspace = offline_flow
    model = OfflineModel(_answer('{"decision":"handoff","output":"完成"}'))
    model.pause_review = mode == 'cancel'
    model.timeout_review = mode == 'timeout'

    async def run():
        manager, service, flow_id = await _start(store, workspace, model)
        try:
            if mode == 'cancel':
                await asyncio.wait_for(model.review_started.wait(), 5)
                service.cancel(flow_id)
            await asyncio.wait_for(service.tasks[flow_id], 8)
            result = service.get(flow_id)['run']
            source = _source(manager, result)
            _assert_receipts(source, 3)
            if mode == 'cancel':
                assert result['status'] == 'cancelled'
                assert all(sid == model.source for sid, *_ in model.calls)
                assert source.task.done()
            else:
                assert result['status'] == 'completed', result
                assert source.record['last_run']['recovery_attempts'] == 1
                assert source.record['last_run']['flow_progress_reviews'][0]['status'] == 'recovery_to_normal'
        finally:
            await manager.shutdown()
    asyncio.run(run())
