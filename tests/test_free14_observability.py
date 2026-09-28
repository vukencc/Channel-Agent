import json

from ai_agent_startup import config


def test_usage_deduplicates_latest_and_preserves_unknown_cost(monkeypatch):
    from ai_agent_startup.core.observability import usage_report
    monkeypatch.setattr(config, 'ENABLE_OBSERVABILITY', True, raising=False)
    last = {'turn_id': 'two', 'model_calls': [{'input_tokens': 10, 'output_tokens': 2, 'tokens_source': 'estimate', 'estimated_cost_usd': None}]}
    first = {'turn_id': 'one', 'model_calls': [{'input_tokens': 20, 'output_tokens': 5, 'tokens_source': 'provider', 'estimated_cost_usd': .01}]}
    result = usage_report({'run_history': [first, last], 'last_run': last})
    assert result['retained_turns'] == 2
    assert result['model_calls'] == 2
    assert result['input_tokens'] == 30
    assert result['known_cost_usd'] == .01
    assert result['total_cost_usd'] is None and result['unknown_cost_calls'] == 1


def test_trace_whitelist_redacts_and_bounds_without_history_access(monkeypatch):
    from ai_agent_startup.core.observability import trace_report
    from ai_agent_startup.core.storage import LazyRecord
    monkeypatch.setattr(config, 'ENABLE_OBSERVABILITY', True, raising=False)
    monkeypatch.setattr(config, 'TRACE_MAX_FIELD_CHARS', 64, raising=False)
    monkeypatch.setattr(config, 'API_KEY', 'secret-api-key-123')
    record = LazyRecord({'last_run': {'turn_id': 'one', 'model_calls': [{'model': 'secret-api-key-123' + 'x' * 500, 'arguments': 'private-body'}],
                         'tools': [{'name': 'read_file', 'content': 'private-document', 'output_chars': 10}]}}, lambda: (_ for _ in ()).throw(AssertionError('不应加载历史')))
    result = trace_report(record)
    text = json.dumps(result, ensure_ascii=False)
    assert 'secret-api-key-123' not in text and 'private-' not in text
    assert len(result[0]['model_calls'][0]['model']) <= 64


def test_cli_observability_default_off_and_enabled(tmp_path, monkeypatch):
    from ai_agent_startup.core.cli import AgentCLI
    from ai_agent_startup.core.storage import SessionStore
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        with create_pipe_input() as pipe:
            cli = AgentCLI(store, input=pipe, output=DummyOutput())
            monkeypatch.setattr(config, 'ENABLE_OBSERVABILITY', False, raising=False)
            cli.handle('/usage')
            assert 'ENABLE_OBSERVABILITY' in cli.notice
            monkeypatch.setattr(config, 'ENABLE_OBSERVABILITY', True)
            cli.handle('/usage')
            assert 'retained_turns' in cli.notice
            cli.handle('/trace')
            assert cli.notice == '[]'
    finally:
        store.close()
