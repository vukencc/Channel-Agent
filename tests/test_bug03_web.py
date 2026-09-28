import json
import importlib

import httpx
import pytest
from pydantic import ValidationError

from ai_agent_startup import config
from ai_agent_startup.tools import sandbox

web = importlib.import_module('ai_agent_startup.tools.web_search')


def test_denial_never_sends_query(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'WEB_SEARCH_API_KEY', 'test-key')
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    sent = []
    def forbidden_request(**kwargs):
        pytest.fail('拒绝确认后不能创建网络客户端')
    monkeypatch.setattr(httpx, 'Client', forbidden_request)
    if hasattr(web, 'tavily_client'):
        monkeypatch.setattr(web.tavily_client, 'search', lambda **kw: sent.append(kw) or {})
    assert '取消' in web.web_search('private query')
    assert sent == []


def test_search_result_count_is_bounded():
    with pytest.raises(ValidationError):
        web.WebSearchArgs(query='test', max_results=11)


def test_network_result_and_timeout_are_readable(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'WEB_SEARCH_API_KEY', 'test-key')
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 100)
    client = httpx.Client
    def response(request):
        assert json.loads(request.content)['query'] == 'test'
        return httpx.Response(200, json={'results': [{'title': 't', 'url': 'https://example.org', 'content': 'x' * 1000}]})
    monkeypatch.setattr(web, 'httpx', httpx, raising=False)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(response), **kw))
    result = web.web_search('test')
    assert len(result) < 150 and '截断' in result
    def timeout(request):
        raise httpx.ReadTimeout('slow')
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(timeout), **kw))
    assert '超时' in web.web_search('test')
