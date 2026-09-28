from ai_agent_startup import config
from ai_agent_startup.core import context


def test_unchanged_message_serialized_once(monkeypatch):
    original = context.json.dumps
    calls = []
    def dumps(value, **kwargs):
        if isinstance(value, list):
            calls.extend(item['content'] for item in value if isinstance(item, dict) and 'role' in item)
        elif isinstance(value, dict) and 'role' in value:
            calls.append(value['content'])
        return original(value, **kwargs)
    monkeypatch.setattr(context.json, 'dumps', dumps)
    history = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'unique'}]
    result, metrics = context.build_model_history(history)
    assert calls.count('unique') == 1
    assert result == history
    assert metrics['sent_chars'] == len(original(history, ensure_ascii=False))
    assert metrics['sent_tokens'] == context.estimate_tokens(original(history, ensure_ascii=False))


def test_token_estimate_matches_character_definition():
    for text in ['', 'abcd中文', 'a😀é\n', 'x' * 10001]:
        ascii_count = sum(ord(char) < 128 for char in text)
        assert context.estimate_tokens(text) == len(text) - ascii_count + (ascii_count + 3) // 4
