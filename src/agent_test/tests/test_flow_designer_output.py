"""离线协议夹具：设计输出恢复与安全导入；不代表真实模型生成或业务执行。"""
import asyncio
import copy
import json

import pytest

from agent_core.core.flow_designer import design_flow
from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.fixture
def offline_designer(tmp_path, monkeypatch, sandbox_env):
    """仅替换模型协议边界，所有文件均隔离在 pytest 临时目录。"""
    store = SessionStore(tmp_path / 'state', sandbox_env)
    manager = SessionManager(store)
    calls = []

    async def run(content, *, failure=None, tool_calls=None):
        async def model(history, *, session_id, emit):
            from agent_core.core.llm import TOOL_SCHEMAS
            from agent_core.core.tool_settings import selected_schemas

            calls.append((session_id, selected_schemas(TOOL_SCHEMAS)))
            if failure is not None:
                raise failure
            return {'role': 'assistant', 'content': content, 'tool_calls': tool_calls}

        monkeypatch.setattr(manager, 'model', model)
        return await design_flow(manager, [{'role': 'user', 'content': '设计后端、前端、测试及反馈复验流程'}])

    try:
        yield run, manager, store, calls
    finally:
        store.close()


@pytest.fixture
def design_payload():
    """反馈以新修订/复验节点展开，避免将运行图伪装成循环调度。"""
    titles = {
        'design': '设计', 'backend': '后端', 'frontend': '前端', 'test': '测试',
        'revise': '反馈修订', 'retest': '复验',
    }
    template = {
        'format': 'agent-flow', 'version': 1, 'title': '离线协议夹具',
        'execution_environment': 'sandbox', 'permission_policy': 'readonly',
        'nodes': [
            {'id': identifier, 'title': title, 'prompt': f'完成{title}并交接真实证据',
             'tool_names': ['read_file', 'list_files']}
            for identifier, title in titles.items()
        ],
        'edges': [
            {'source': source, 'target': target}
            for source, target in [
                ('design', 'backend'), ('design', 'frontend'), ('backend', 'test'),
                ('frontend', 'test'), ('test', 'revise'), ('revise', 'retest'),
            ]
        ],
    }
    return {'reply': '已设计，反馈修订后独立复验。', 'template': template}


@pytest.mark.parametrize('wrapper', ['strict', 'fenced-json', 'fenced', 'single-object'], ids=str)
def test_design_output_recovers_one_complete_object_and_imports_without_execution(
    offline_designer, design_payload, wrapper, sandbox_env,
):
    run, manager, store, calls = offline_designer
    raw = json.dumps(design_payload, ensure_ascii=False)
    contents = {
        'strict': raw, 'fenced-json': f'```json\n{raw}\n```',
        'fenced': f'```\n{raw}\n```', 'single-object': f'设计结果如下：\n{raw}\n以上为完整结果。',
    }
    sentinel = sandbox_env / 'business.txt'
    sentinel.write_text('用户输入保持原样', encoding='utf-8')
    before = {path.relative_to(sandbox_env): path.read_bytes() for path in sandbox_env.rglob('*') if path.is_file()}
    result = asyncio.run(run(contents[wrapper]))
    assert result['reply'] == design_payload['reply']
    assert not (store.root / 'flows').exists()
    service = FlowService(manager)
    imported = service.import_template(result['template'])
    assert {node['id'] for node in imported['nodes']} == {node['id'] for node in design_payload['template']['nodes']}
    assert imported['edges'] == design_payload['template']['edges']
    assert imported['permission_policy'] == 'readonly'
    assert imported['execution_environment'] == 'sandbox'
    assert imported['workspace_path'] is None
    assert imported['project_id'] is None
    assert not imported['session_bindings']
    assert not service.tasks
    assert all(node['tool_names'] == ['read_file', 'list_files'] for node in imported['nodes'])
    assert calls == [(None, [])]
    assert {path.relative_to(sandbox_env): path.read_bytes() for path in sandbox_env.rglob('*') if path.is_file()} == before


@pytest.mark.parametrize('fenced', [False, True], ids=['strict', 'fenced-json'])
def test_design_output_clarification_preserves_null(offline_designer, fenced):
    run, _, store, calls = offline_designer
    raw = json.dumps({'reply': '请说明验收标准。', 'template': None}, ensure_ascii=False)
    result = asyncio.run(run(f'```json\n{raw}\n```' if fenced else raw))
    assert result == {'reply': '请说明验收标准。', 'template': None}
    assert not (store.root / 'flows').exists()
    assert calls == [(None, [])]


@pytest.mark.parametrize('kind', [
    'two-objects', 'other-object', 'two-fences', 'truncated',
    'embedded-string', 'quoted-object', 'array',
    'cycle', 'node-workspace', 'template-workspace', 'node-credentials',
    'unknown-tool', 'unknown-model', 'reply-extra',
])
@pytest.mark.parametrize('fenced', [False, True], ids=['strict', 'fenced-json'])
def test_design_output_rejects_ambiguous_or_unsafe_candidates(offline_designer, design_payload, kind, fenced):
    run, _, store, calls = offline_designer
    candidate = copy.deepcopy(design_payload)
    if kind == 'cycle':
        candidate['template']['edges'].append({'source': 'retest', 'target': 'design'})
    elif kind == 'node-workspace':
        candidate['template']['nodes'][0]['workspace_path'] = '/tmp/forbidden'
    elif kind == 'template-workspace':
        candidate['template']['workspace_path'] = '/tmp/forbidden'
    elif kind == 'node-credentials':
        candidate['template']['nodes'][0]['api_key'] = 'offline-protocol-marker'
    elif kind == 'unknown-tool':
        candidate['template']['nodes'][0]['tool_names'] = ['unavailable_protocol_tool']
    elif kind == 'unknown-model':
        candidate['template']['model_profile'] = 'unavailable-protocol-profile'
    elif kind == 'reply-extra':
        candidate['execute'] = True
    raw = json.dumps(candidate, ensure_ascii=False)
    content = {
        'two-objects': raw + '\n' + raw,
        'other-object': '{}\n' + raw,
        'two-fences': f'```json\n{raw}\n```\n```json\n{raw}\n```',
        'truncated': raw[:-5],
        'embedded-string': json.dumps({'documentation': raw}),
        'quoted-object': json.dumps(raw),
        'array': '[' + raw + ']',
    }.get(kind, raw)
    if fenced:
        content = f'```json\n{content}\n```'
    with pytest.raises(ValueError):
        asyncio.run(run(content))
    assert calls == [(None, [])]
    assert not (store.root / 'flows').exists()


@pytest.mark.parametrize('failure', [asyncio.CancelledError, TimeoutError], ids=['cancelled', 'timeout'])
def test_design_output_never_recovers_after_cancel_or_timeout(offline_designer, design_payload, failure):
    run, _, store, calls = offline_designer
    expected = asyncio.CancelledError if failure is asyncio.CancelledError else ValueError
    with pytest.raises(expected):
        asyncio.run(run(json.dumps(design_payload), failure=failure()))
    assert calls == [(None, [])]
    assert not (store.root / 'flows').exists()


def test_design_output_cancels_pending_model_without_import(offline_designer, monkeypatch):
    _, manager, store, _ = offline_designer

    async def exercise():
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def model(history, *, session_id, emit):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        monkeypatch.setattr(manager, 'model', model)
        task = asyncio.create_task(design_flow(manager, [{'role': 'user', 'content': '设计流程'}]))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert stopped.is_set()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert not (store.root / 'flows').exists()


@pytest.mark.parametrize('syntax', ['trailing-comma', 'literal-newline'])
def test_design_output_repairs_completed_syntax_once_with_same_model(
    offline_designer, design_payload, monkeypatch, syntax, sandbox_env,
):
    from agent_core import config
    from agent_core.core.llm import TOOL_SCHEMAS
    from agent_core.core.model_settings import active_endpoint, active_profile, active_reasoning_effort
    from agent_core.core.tool_settings import selected_schemas

    _, manager, store, _ = offline_designer
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 1)
    profile = manager.model_registry.add(
        name='离线格式修正', model='offline-protocol-model', base_url='https://example.invalid/v1',
    )['id']
    raw = json.dumps(design_payload, ensure_ascii=False)
    malformed = raw[:-1] + ',}' if syntax == 'trailing-comma' else raw.replace('已设计，', '已设计，\n')
    calls = []

    async def model(history, *, session_id, emit):
        calls.append((copy.deepcopy(history), active_profile.get(), active_endpoint.get(),
                      active_reasoning_effort.get(), session_id, selected_schemas(TOOL_SCHEMAS)))
        assert len(calls) <= 2
        return {'role': 'assistant', 'content': malformed if len(calls) == 1 else raw}

    monkeypatch.setattr(manager, 'model', model)
    result = asyncio.run(design_flow(
        manager, [{'role': 'user', 'content': '设计开发测试及反馈复验'}],
        model_profile=profile, reasoning_effort='xhigh',
    ))
    assert len(calls) == 2
    assert calls[0][1:] == calls[1][1:]
    assert calls[0][1] == profile
    assert calls[0][3:] == ('xhigh', None, [])
    assert calls[0][0] != calls[1][0]
    assert not (store.root / 'flows').exists()
    imported = FlowService(manager).import_template(result['template'])
    assert imported['permission_policy'] == 'readonly'
    assert len(imported['nodes']) == 6
    assert not list(sandbox_env.iterdir())


@pytest.mark.parametrize('recovery_limit, expected_calls', [(0, 1), (1, 2), (3, 2)])
def test_design_output_persistent_syntax_error_is_bounded_and_redacted(
    offline_designer, monkeypatch, recovery_limit, expected_calls,
):
    from agent_core import config

    _, manager, store, _ = offline_designer
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', recovery_limit)
    calls = []
    marker = 'offline-private-draft-marker'

    async def model(history, *, session_id, emit):
        calls.append(copy.deepcopy(history))
        assert len(calls) <= 2
        return {'role': 'assistant', 'content': '{"reply":"' + marker + '","template":null,}'}

    monkeypatch.setattr(manager, 'model', model)
    with pytest.raises(ValueError) as error:
        asyncio.run(design_flow(manager, [{'role': 'user', 'content': '设计流程'}]))
    assert len(calls) == expected_calls
    assert marker not in str(error.value)
    assert not (store.root / 'flows').exists()


def test_design_output_does_not_send_repair_when_input_budget_cannot_fit(
    offline_designer, design_payload, monkeypatch,
):
    from agent_core import config
    from agent_core.core.context import history_size

    _, manager, store, _ = offline_designer
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 1)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 24000)
    candidate = copy.deepcopy(design_payload)
    candidate['reply'] = 'x' * 16000
    malformed = json.dumps(candidate)[:-1] + ',}'
    calls = []

    async def model(history, *, session_id, emit):
        calls.append(copy.deepcopy(history))
        assert history_size(history) < 24000, '离线模型边界不得收到超出输入预算的修正请求'
        return {'role': 'assistant', 'content': malformed}

    monkeypatch.setattr(manager, 'model', model)
    with pytest.raises(ValueError) as error:
        asyncio.run(design_flow(manager, [{'role': 'user', 'content': '设计流程'}]))
    assert len(calls) == 1
    assert '输入' in str(error.value) or '预算' in str(error.value)
    assert not (store.root / 'flows').exists()


@pytest.mark.parametrize('failure', ['cancelled', 'budget'])
def test_design_output_repair_failure_never_starts_third_request(
    offline_designer, design_payload, monkeypatch, failure,
):
    from agent_core import config
    from agent_core.core.budgets import BudgetExceeded

    _, manager, store, _ = offline_designer
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 3)
    calls = []

    async def model(history, *, session_id, emit):
        calls.append(copy.deepcopy(history))
        if len(calls) == 1:
            return {'role': 'assistant', 'content': json.dumps(design_payload)[:-1] + ',}'}
        assert len(calls) == 2
        if failure == 'cancelled':
            raise asyncio.CancelledError()
        raise BudgetExceeded('offline-budget-marker')

    monkeypatch.setattr(manager, 'model', model)
    expected = asyncio.CancelledError if failure == 'cancelled' else ValueError
    with pytest.raises(expected):
        asyncio.run(design_flow(manager, [{'role': 'user', 'content': '设计流程'}]))
    assert len(calls) == 2
    assert not (store.root / 'flows').exists()


def test_design_output_does_not_repair_tool_call_or_budget_failure(offline_designer, monkeypatch):
    from agent_core import config
    from agent_core.core.budgets import BudgetExceeded

    run, _, store, calls = offline_designer
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 3)
    malformed = '{"reply":"离线协议夹具","template":null,}'
    with pytest.raises(ValueError, match='不能调用工具'):
        asyncio.run(run(malformed, tool_calls=[{'function': {'name': 'read_file', 'arguments': '{}'}}]))
    assert calls == [(None, [])]
    with pytest.raises(ValueError):
        asyncio.run(run(malformed, failure=BudgetExceeded('offline-budget-marker')))
    assert calls == [(None, []), (None, [])]
    assert not (store.root / 'flows').exists()
