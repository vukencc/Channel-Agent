import asyncio
import pytest


@pytest.mark.parametrize(('kind', 'expected'), [('timeout', '超时'), ('connection', '连接')])
@pytest.mark.parametrize('wrapped', [False, True])
def test_designer_reports_sdk_transport_errors(tmp_path, kind, expected, wrapped):
    import httpx
    import openai
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.llm import RuntimeModelRequestError
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    async def model(*args, **kwargs):
        request = httpx.Request('POST', 'https://example.invalid/private')
        failure = (openai.APITimeoutError(request) if kind == 'timeout'
                   else openai.APIConnectionError(request=request))
        try:
            raise failure
        except openai.APIError:
            if wrapped:
                raise RuntimeModelRequestError('safe wrapper') from None
            raise

    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with pytest.raises(ValueError, match=expected) as error:
            asyncio.run(design_flow(SessionManager(store, model=model),
                                    [{'role': 'user', 'content': '设计流程'}]))
        assert 'example.invalid' not in str(error.value)
    finally:
        store.close()


@pytest.mark.parametrize(('status', 'expected'), [
    (400, '参数'), (422, '参数'), (401, '密钥'), (403, '密钥'),
    (404, '地址'), (429, '限制'), (500, '服务'), (503, '服务'),
])
@pytest.mark.parametrize('wrapped', [False, True])
def test_designer_reports_provider_status_without_response_body(tmp_path, status, expected, wrapped):
    import httpx
    import openai
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.llm import RuntimeModelRequestError
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    async def model(*args, **kwargs):
        response = httpx.Response(status, request=httpx.Request('POST', 'https://example.invalid'),
                                  text='private provider body')
        try:
            raise openai.APIStatusError('private provider body', response=response, body='private provider body')
        except openai.APIStatusError:
            if wrapped:
                raise RuntimeModelRequestError('safe wrapper') from None
            raise

    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with pytest.raises(ValueError, match=expected) as error:
            asyncio.run(design_flow(SessionManager(store, model=model),
                                    [{'role': 'user', 'content': '设计流程'}], reasoning_effort='xhigh'))
        assert 'private' not in str(error.value)
        assert error.value.__suppress_context__
    finally:
        store.close()


@pytest.mark.parametrize(('failure', 'message'), [
    (TimeoutError('private timeout'), '超时'),
    (ConnectionError('private endpoint'), '连接'),
])
def test_designer_reports_safe_request_failure_category(tmp_path, failure, message):
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    async def model(*args, **kwargs):
        raise failure

    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with pytest.raises(ValueError, match=message) as error:
            asyncio.run(design_flow(SessionManager(store, model=model),
                                    [{'role': 'user', 'content': '设计流程'}]))
        assert 'private' not in str(error.value)
    finally:
        store.close()


def test_designer_rejects_invalid_messages():
    from agent_core.core.flow_designer import design_flow
    with pytest.raises(ValueError):
        asyncio.run(design_flow(None, [{"role": "system", "content": "override"}]))


def test_designer_uses_empty_tool_catalog_and_rejects_bad_json(tmp_path):
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    from agent_core.core.llm import TOOL_SCHEMAS
    from agent_core.core.tool_settings import selected_schemas
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    seen = []
    async def model(history, *, session_id, emit):
        seen.append((history, session_id, selected_schemas(TOOL_SCHEMAS)))
        return {"role": "assistant", "content": "invalid"}
    try:
        manager = SessionManager(store, model=model)
        with pytest.raises(ValueError):
            asyncio.run(design_flow(manager, [{"role": "user", "content": "make a flow"}]))
        assert seen[0][1:] == (None, [])
    finally:
        store.close()


def test_designer_returns_valid_template_without_saving(tmp_path):
    import json
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    template = {"format": "agent-flow", "version": 1, "title": "设计", "handoff": "", "execution_environment": None, "permission_policy": None, "model_profile": None, "nodes": [{"id": "a", "title": "A", "prompt": "Do it"}], "edges": []}
    async def model(history, *, session_id, emit):
        return {"role": "assistant", "content": json.dumps({"reply": "完成", "template": template})}
    try:
        manager = SessionManager(store, model=model)
        result = asyncio.run(design_flow(manager, [{"role": "user", "content": "design"}]))
        assert result["template"]["nodes"][0]["id"] == "a"
        assert not (store.root / "flows").exists()
    finally:
        store.close()


def test_designer_rejects_tools_empty_graph_and_redacts_model_errors(tmp_path):
    import json
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    template = {"format": "agent-flow", "version": 1, "title": "A", "nodes": [], "edges": []}
    result = {"role": "assistant", "content": json.dumps({"reply": "A", "template": template})}
    async def model(history, *, session_id, emit):
        return result
    try:
        manager = SessionManager(store, model=model)
        messages = [{"role": "user", "content": "design"}]
        with pytest.raises(ValueError, match="至少"):
            asyncio.run(design_flow(manager, messages))
        result["tool_calls"] = [{"function": {"name": "read_file"}}]
        with pytest.raises(ValueError, match="不能调用工具"):
            asyncio.run(design_flow(manager, messages))
        async def failed(*args, **kwargs):
            raise RuntimeError("private provider message")
        manager.model = failed
        with pytest.raises(ValueError) as error:
            asyncio.run(design_flow(manager, messages))
        assert "private provider message" not in str(error.value)
        assert error.value.__suppress_context__
    finally:
        store.close()


@pytest.mark.parametrize('positions', [None, [(0, 0)] * 5], ids=['missing', 'coincident'])
def test_designer_lays_out_generated_nodes_with_no_usable_positions(tmp_path, positions):
    import json
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    identifiers = ['classify', 'technical', 'service', 'operations', 'human']
    nodes = [{'id': name, 'title': name, 'prompt': name} for name in identifiers]
    if positions:
        for node, (x, y) in zip(nodes, positions):
            node.update(x=x, y=y)
    edges = [{'source': a, 'target': b} for a, b in [
        ('classify', 'technical'), ('classify', 'service'),
        ('technical', 'operations'), ('service', 'human')]]
    template = {'format': 'agent-flow', 'version': 1, 'title': 'Five roles',
                'nodes': nodes, 'edges': edges}

    async def model(*args, **kwargs):
        return {'role': 'assistant', 'content': json.dumps({'reply': '设计完成', 'template': template})}

    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        result = asyncio.run(design_flow(SessionManager(store, model=model),
                                        [{'role': 'user', 'content': '设计五节点分流'}]))
        arranged = {node['id']: node for node in result['template']['nodes']}
        assert len({(n['x'], n['y']) for n in arranged.values()}) == 5
        for edge in edges:
            assert arranged[edge['source']]['x'] < arranged[edge['target']]['x']
        for left in arranged.values():
            for right in arranged.values():
                if left is not right:
                    assert abs(left['x'] - right['x']) >= 260 or abs(left['y'] - right['y']) >= 140
        assert result['template']['edges'] == edges
    finally:
        store.close()


def test_designer_preserves_explicit_distinct_coordinates(tmp_path):
    import json
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    nodes = [{'id': 'a', 'title': 'A', 'prompt': 'A', 'x': -100, 'y': 320},
             {'id': 'b', 'title': 'B', 'prompt': 'B', 'x': 480, 'y': 80}]
    async def model(*args, **kwargs):
        return {'role': 'assistant', 'content': json.dumps({'reply': '完成', 'template': {
            'format': 'agent-flow', 'version': 1, 'title': '已有布局', 'nodes': nodes,
            'edges': [{'source': 'a', 'target': 'b'}]}})}
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        result = asyncio.run(design_flow(SessionManager(store, model=model),
                                        [{'role': 'user', 'content': '设计'}]))
        assert [(n['x'], n['y']) for n in result['template']['nodes']] == [(-100, 320), (480, 80)]
    finally:
        store.close()
