"""独立生成预算的离线 SDK/SSE 回归；受控服务，不调用真实供应商。"""
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from agent_core import config
from agent_core.core import llm
from agent_core.core import model_settings
from agent_core.core.budgets import BudgetLedger
from agent_core.core.flow_designer import design_flow
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


TOTAL_CHARS = 192000
BODY_CHARS = 48000


def _template():
    nodes = []
    for index in range(5):
        tools = [] if index == 0 else [{
            'name': f'controlled_tool_{index}', 'description': '读取受控输入',
            'parameters': {'type': 'object', 'properties': {}},
            'code': 'def main(arguments):\n    return arguments\n',
        }]
        nodes.append({'id': f'n{index}', 'title': f'角色{index}', 'prompt': f'完整角色{index}',
                      'reasoning_effort': 'xhigh', 'custom_tools': tools})
    return {'format': 'agent-flow', 'version': 1, 'title': '五节点四动态工具',
            'permission_policy': 'readonly', 'nodes': nodes,
            'edges': [{'source': f'n{i}', 'target': f'n{i + 1}'} for i in range(4)]}


class GenerationSSE(httpx.AsyncByteStream):
    def __init__(self, *, reasoning_chars=BODY_CHARS + 1, body=None, usage=None, hold=False,
                 arguments=None):
        self.reasoning_chars = reasoning_chars
        self.body = body if body is not None else json.dumps(
            {'reply': '设计完成', 'template': _template()}, ensure_ascii=False)
        self.usage, self.hold, self.arguments = usage, hold, arguments
        self.closed = False
        self.body_emitted = False
        self.reasoning_processed = asyncio.Event()
        self.release = asyncio.Event()

    @staticmethod
    def frame(delta, finish=None, *, usage=None):
        payload = {'id': 'controlled-generation', 'object': 'chat.completion.chunk',
                   'created': 0, 'model': 'controlled-generation', 'choices': [
                       {'index': 0, 'delta': delta, 'finish_reason': finish}]}
        if usage is not None:
            payload['choices'] = []
            payload['usage'] = usage
        return ('data: ' + json.dumps(payload) + '\n\n').encode()

    async def __aiter__(self):
        yield self.frame({'reasoning_content': '想' * self.reasoning_chars})
        self.reasoning_processed.set()
        if self.hold:
            await self.release.wait()
        self.body_emitted = True
        if self.arguments is not None:
            yield self.frame({'content': self.body, 'tool_calls': [{'index': 0, 'id': 'controlled-call', 'type': 'function',
                'function': {'name': 'read_file', 'arguments': self.arguments}}]}, 'tool_calls')
        else:
            yield self.frame({'content': self.body}, 'stop')
        if self.usage:
            yield self.frame({}, usage=self.usage)
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed = True


@pytest.fixture
def generation_env(tmp_path, sandbox_env, monkeypatch):
    for name, value in {'MODEL_OUTPUT_CHARS': BODY_CHARS, 'MODEL_CALL_TIMEOUT': 90,
                        'MODEL_CONTEXT_TOKENS': 0, 'MODEL_RESPONSE_RESERVE_TOKENS': 0,
                        'SESSION_COST_LIMIT': 0, 'DAILY_COST_LIMIT': 0,
                        'MODEL_REQUESTS_PER_MINUTE': 0, 'INPUT_COST_PER_MILLION': 0,
                        'OUTPUT_COST_PER_MILLION': 0, 'MAX_RETRIES': 1,
                        'MODEL_STREAM_USAGE': True}.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, 'MODEL', 'controlled-generation')
    monkeypatch.setattr(config, 'FLOW_DESIGNER_TOTAL_OUTPUT_CHARS', TOTAL_CHARS)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    manager = SessionManager(store, model=llm.call_model)
    manager.budget_ledger = BudgetLedger(store.root)
    profile = manager.model_registry.add(name='受控生成', model='controlled-generation',
        base_url='https://controlled.invalid/v1', api_key='fixture-key')['id']
    requests, streams, clients, metrics, reservations, scopes = [], [], [], [], [], []
    factory = GenerationSSE

    def install(value):
        nonlocal factory
        factory = value

    def handle(request):
        requests.append(json.loads(request.content))
        reservations.append(dict(manager.budget_ledger.state['pending']))
        stream = factory()
        streams.append(stream)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

    def client(endpoint):
        sdk = llm.AsyncOpenAI(api_key='fixture-key', base_url='https://controlled.invalid/v1',
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        clients.append(sdk)
        return sdk

    async def observed(history, *, session_id, emit):
        scopes.append(model_settings.active_total_output_chars.get())
        def observe(kind, value):
            if kind == 'metrics':
                metrics.append(value)
            emit(kind, value)
        return await llm.call_model(history, session_id=session_id, emit=observe)

    monkeypatch.setattr(llm, 'endpoint_client', client)
    monkeypatch.setattr(manager, 'model', observed)
    env = SimpleNamespace(manager=manager, profile=profile, requests=requests, streams=streams,
                          clients=clients, metrics=metrics, install=install, reservations=reservations)
    env.scopes = scopes
    yield env
    store.close()


async def _design(env):
    return await design_flow(env.manager, [{'role': 'user', 'content': '保留完整五节点四动态工具及业务规则'}],
                             model_profile=env.profile, reasoning_effort='xhigh')


def _closed_without_import(env):
    assert all(stream.closed for stream in env.streams)
    assert all(client.is_closed() for client in env.clients)
    assert env.manager.budget_ledger.state['pending'] == {}
    assert not env.manager.sessions
    assert not (env.manager.store.root / 'flows').exists()


def test_designer_long_reasoning_finishes_full_template_with_independent_budget(generation_env):
    env = generation_env
    result = asyncio.run(_design(env))
    assert len(result['template']['nodes']) == 5
    assert sum(len(node['custom_tools']) for node in result['template']['nodes']) == 4
    assert all(node['reasoning_effort'] == 'xhigh' for node in result['template']['nodes'])
    assert env.requests[0]['reasoning_effort'] == 'xhigh'
    assert env.requests[0]['model'] == 'controlled-generation'
    assert env.requests[0]['messages'][1:] == [
        {'role': 'user', 'content': '保留完整五节点四动态工具及业务规则'}]
    assert 'tools' not in env.requests[0]
    assert 'max_tokens' not in env.requests[0]
    assert env.metrics[-1]['reasoning_chars'] == BODY_CHARS + 1
    assert config.MODEL_OUTPUT_CHARS == BODY_CHARS
    assert config.MODEL_CALL_TIMEOUT == 90
    _closed_without_import(env)


@pytest.mark.parametrize('reasoning_chars', [TOTAL_CHARS, TOTAL_CHARS + 1])
def test_designer_total_cap_bounds_reasoning_before_any_body(generation_env, reasoning_chars):
    env = generation_env
    env.install(lambda: GenerationSSE(reasoning_chars=reasoning_chars))
    with pytest.raises(ValueError, match='未返回完整可用内容'):
        asyncio.run(_design(env))
    assert env.metrics[-1]['reasoning_chars'] == reasoning_chars
    assert env.metrics[-1]['content_chars'] == (0 if reasoning_chars > TOTAL_CHARS else len(env.streams[0].body))
    assert env.streams[0].body_emitted is (reasoning_chars == TOTAL_CHARS)
    _closed_without_import(env)


def test_designer_combined_generation_total_allows_exact_boundary(generation_env):
    env = generation_env
    body = GenerationSSE(reasoning_chars=0).body
    env.install(lambda: GenerationSSE(reasoning_chars=TOTAL_CHARS - len(body), body=body))
    assert asyncio.run(_design(env))['template']['title'] == '五节点四动态工具'
    assert env.metrics[-1]['reasoning_chars'] + env.metrics[-1]['content_chars'] == TOTAL_CHARS
    _closed_without_import(env)


@pytest.mark.parametrize('kind', ['body', 'arguments', 'body-plus-arguments'])
def test_designer_body_and_arguments_still_have_original_cap(generation_env, kind):
    env = generation_env
    if kind == 'body':
        env.install(lambda: GenerationSSE(reasoning_chars=1, body='x' * (BODY_CHARS + 1)))
    elif kind == 'arguments':
        env.install(lambda: GenerationSSE(reasoning_chars=1, body='', arguments='x' * (BODY_CHARS + 1)))
    else:
        env.install(lambda: GenerationSSE(reasoning_chars=1, body='x' * (BODY_CHARS - 1), arguments='{}'))
    with pytest.raises(ValueError, match='未返回完整可用内容'):
        asyncio.run(_design(env))
    assert env.metrics[-1]['content_chars'] + env.metrics[-1]['argument_chars'] == BODY_CHARS + 1
    assert env.streams[0].body_emitted
    _closed_without_import(env)


async def _ordinary(env):
    from agent_core.core.model_settings import model_profile
    from agent_core.core.tool_settings import model_tools

    with model_profile(env.profile, env.manager.model_registry.resolve(env.profile)), model_tools([]):
        return await llm.call_model([{'role': 'user', 'content': '普通请求'}], emit=lambda *a: None)


@pytest.mark.parametrize('kind', ['reasoning', 'reasoning-plus-body'])
def test_ordinary_combined_output_cap_is_unchanged(generation_env, kind):
    env = generation_env
    if kind == 'reasoning-plus-body':
        env.install(lambda: GenerationSSE(reasoning_chars=24000, body='x' * 24001))
    with pytest.raises(llm.ModelResponseError, match='字符上限'):
        asyncio.run(_ordinary(env))
    assert env.streams[0].body_emitted is (kind == 'reasoning-plus-body')
    _closed_without_import(env)


def test_ordinary_combined_output_allows_exact_original_boundary(generation_env):
    env = generation_env
    env.install(lambda: GenerationSSE(reasoning_chars=24000, body='x' * 24000))
    assert asyncio.run(_ordinary(env))['content'] == 'x' * 24000
    _closed_without_import(env)


def test_generation_scope_is_isolated_concurrently_and_restored_on_exit(generation_env):
    env = generation_env
    env.install(lambda: GenerationSSE(hold=not env.streams))

    async def run():
        task = asyncio.create_task(_design(env))
        try:
            async with asyncio.timeout(5):
                while not env.streams:
                    await asyncio.sleep(.001)
                await env.streams[0].reasoning_processed.wait()
                assert model_settings.active_total_output_chars.get() is None
                assert llm.effective_total_output_chars() == BODY_CHARS
                with pytest.raises(llm.ModelResponseError):
                    await _ordinary(env)
                env.streams[0].release.set()
                assert (await task)['template']['title'] == '五节点四动态工具'
                assert model_settings.active_total_output_chars.get() is None
                assert llm.effective_total_output_chars() == BODY_CHARS
                with pytest.raises(llm.ModelResponseError):
                    await _ordinary(env)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert env.scopes == [TOTAL_CHARS]
    assert len(env.requests) == len(env.streams) == len(env.clients) == 3
    _closed_without_import(env)


def _known_prices(monkeypatch):
    monkeypatch.setattr(config, 'INPUT_COST_PER_MILLION', 1)
    monkeypatch.setattr(config, 'OUTPUT_COST_PER_MILLION', 2)


def _reserved(env):
    assert len(env.reservations) == 1
    pending = env.reservations[0]
    assert len(pending) == 1
    return next(iter(pending.values()))['reserved']


def _expected_reserve(env, output_tokens=TOTAL_CHARS):
    from agent_core.core.context import request_tokens
    return (request_tokens(env.requests[0]['messages'], []) + output_tokens * 2) / 1e6


def test_known_price_reserves_total_generation_without_usage_discount(generation_env, monkeypatch):
    env = generation_env
    _known_prices(monkeypatch)
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1)
    assert asyncio.run(_design(env))['template']['nodes']
    assert _reserved(env) == pytest.approx(_expected_reserve(env))
    assert env.manager.budget_ledger.state['sessions']['flow-designer'] == pytest.approx(_reserved(env))
    assert sum(env.manager.budget_ledger.state['days'].values()) == pytest.approx(_reserved(env))
    assert env.metrics[-1]['tokens_source'] == 'estimate'
    _closed_without_import(env)


def test_known_price_insufficient_budget_blocks_before_http(generation_env, monkeypatch):
    env = generation_env
    _known_prices(monkeypatch)
    # 0.2 足够旧 48k 预留，无法支付新的 192k 总生成预留，必须在 HTTP 前阻止。
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', .2)
    with pytest.raises(ValueError, match='预算'):
        asyncio.run(_design(env))
    assert not env.requests and not env.streams
    assert env.clients
    _closed_without_import(env)


def test_ordinary_cost_reservation_keeps_original_limit_after_design(generation_env, monkeypatch):
    from agent_core.core.budgets import budget_scope
    from agent_core.core.context import request_tokens

    env = generation_env
    _known_prices(monkeypatch)
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1)

    async def run():
        assert (await _design(env))['template']['nodes']
        with budget_scope(env.manager.budget_ledger, 'ordinary'):
            with pytest.raises(llm.ModelResponseError):
                await _ordinary(env)

    asyncio.run(run())
    assert len(env.reservations) == 2
    ordinary_reserve = next(iter(env.reservations[1].values()))['reserved']
    expected = (request_tokens(env.requests[1]['messages'], []) + BODY_CHARS * 2) / 1e6
    assert ordinary_reserve == pytest.approx(expected)
    assert env.manager.budget_ledger.state['sessions']['ordinary'] == pytest.approx(expected)
    _closed_without_import(env)


@pytest.mark.parametrize('budget_enabled', [False, True])
def test_unknown_price_never_bypasses_selected_cost_budget(generation_env, monkeypatch, budget_enabled):
    env = generation_env
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1 if budget_enabled else 0)
    if budget_enabled:
        with pytest.raises(ValueError, match='预算'):
            asyncio.run(_design(env))
        assert not env.requests
    else:
        assert asyncio.run(_design(env))['template']['nodes']
    _closed_without_import(env)


def test_explicit_max_tokens_is_respected_for_request_and_reservation(generation_env, monkeypatch):
    env = generation_env
    _known_prices(monkeypatch)
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1)
    env.manager.model_registry.update(env.profile, name='受控生成', model='controlled-generation',
        base_url='https://controlled.invalid/v1', parameters={'max_tokens': 4096})
    assert asyncio.run(_design(env))['template']['nodes']
    assert env.requests[0]['max_tokens'] == 4096
    assert env.requests[0]['reasoning_effort'] == 'xhigh'
    assert _reserved(env) == pytest.approx(_expected_reserve(env, 4096))
    _closed_without_import(env)


def test_provider_usage_takes_priority_over_larger_reservation(generation_env, monkeypatch):
    env = generation_env
    _known_prices(monkeypatch)
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1)
    env.install(lambda: GenerationSSE(usage={'prompt_tokens': 13, 'completion_tokens': 17,
                                           'total_tokens': 30}))
    assert asyncio.run(_design(env))['template']['nodes']
    assert _reserved(env) == pytest.approx(_expected_reserve(env))
    assert env.manager.budget_ledger.state['sessions']['flow-designer'] == pytest.approx(47 / 1e6)
    assert sum(env.manager.budget_ledger.state['days'].values()) == pytest.approx(47 / 1e6)
    assert env.metrics[-1]['tokens_source'] == 'provider'
    _closed_without_import(env)


def test_stop_after_long_reasoning_drains_and_retains_reserved_cost(generation_env, monkeypatch):
    env = generation_env
    _known_prices(monkeypatch)
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 1)
    env.install(lambda: GenerationSSE(hold=True))

    async def run():
        env.manager.slots = asyncio.Semaphore(1)
        task = asyncio.create_task(_design(env))
        try:
            async with asyncio.timeout(5):
                while not env.streams:
                    await asyncio.sleep(.001)
                await env.streams[0].reasoning_processed.wait()
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert env.manager.slots._value == 1
        assert model_settings.active_total_output_chars.get() is None

    asyncio.run(run())
    assert not env.streams[0].body_emitted
    assert _reserved(env) == pytest.approx(_expected_reserve(env))
    assert env.manager.budget_ledger.state['sessions']['flow-designer'] == pytest.approx(_reserved(env))
    _closed_without_import(env)


@pytest.mark.parametrize('value', ['0', '-1', '1048577', '1.5', 'nan', 'inf', 'true', '1e5'])
def test_invalid_designer_generation_cap_fails_configuration_import(tmp_path, value):
    env = {**os.environ, 'AI_AGENT_PROJECT_ROOT': str(tmp_path), 'PYTHON_DOTENV_DISABLED': '1',
           'FLOW_DESIGNER_TOTAL_OUTPUT_CHARS': value}
    result = subprocess.run([sys.executable, '-c', 'from agent_core import config'], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'FLOW_DESIGNER_TOTAL_OUTPUT_CHARS' in result.stderr


@pytest.mark.parametrize(('value', 'expected'), [(None, TOTAL_CHARS), ('1', 1), ('1048576', 1048576)])
def test_designer_generation_configuration_has_finite_default_and_bounds(tmp_path, value, expected):
    env = {**os.environ, 'AI_AGENT_PROJECT_ROOT': str(tmp_path), 'PYTHON_DOTENV_DISABLED': '1'}
    env.pop('FLOW_DESIGNER_TOTAL_OUTPUT_CHARS', None)
    if value is not None:
        env['FLOW_DESIGNER_TOTAL_OUTPUT_CHARS'] = value
    result = subprocess.run([sys.executable, '-c',
                             'from agent_core import config; print(config.FLOW_DESIGNER_TOTAL_OUTPUT_CHARS)'],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == expected


@pytest.mark.parametrize('value', [0, -1, 1048577, True, False, 1.5, float('inf'), float('nan'), '192000', None])
def test_generation_scope_rejects_invalid_limits_without_changing_outer_scope(value):
    with model_settings.model_total_output_chars(1000):
        with pytest.raises(ValueError):
            with model_settings.model_total_output_chars(value):
                pytest.fail('无效生成预算被接受')
        assert model_settings.active_total_output_chars.get() == 1000
        assert llm.effective_total_output_chars() == 1000
    assert model_settings.active_total_output_chars.get() is None


@pytest.mark.parametrize('value', [1, TOTAL_CHARS, 1048576])
def test_generation_scope_valid_boundaries_restore_after_failure(value, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_OUTPUT_CHARS', BODY_CHARS)
    with pytest.raises(RuntimeError, match='受控失败'):
        with model_settings.model_total_output_chars(value):
            assert llm.effective_total_output_chars() == value
            raise RuntimeError('受控失败')
    assert model_settings.active_total_output_chars.get() is None
    assert llm.effective_total_output_chars() == BODY_CHARS


def test_designer_uses_configured_total_instead_of_hardcoded_default(generation_env, monkeypatch):
    env = generation_env
    monkeypatch.setattr(config, 'FLOW_DESIGNER_TOTAL_OUTPUT_CHARS', 128)
    env.install(lambda: GenerationSSE(reasoning_chars=129))
    with pytest.raises(ValueError, match='未返回完整可用内容'):
        asyncio.run(_design(env))
    assert env.scopes == [128]
    assert not env.streams[0].body_emitted
    assert model_settings.active_total_output_chars.get() is None
    _closed_without_import(env)
