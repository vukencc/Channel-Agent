import json

import pytest

from ai_agent_startup.core.storage import SessionStore


def test_export_saved_does_not_materialize_history(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        record = store.create('test', 'system')
        record['messages'].append({'role': 'user', 'content': 'hello\n世界'})
        store.save(record)
        expected = store.read_record(store.directory(record['id']) / 'session.json')
        expected['memory'] = ''
        def forbidden(*args):
            pytest.fail('导出不应读取完整历史')
        monkeypatch.setattr(store, 'read_record', forbidden)
        result = store.export_saved(record['id'], 'json').read_text()
        assert result == json.dumps(expected, ensure_ascii=False, indent=2) + '\n'
        assert 'hello\n世界' in store.export_saved(record['id'], 'md').read_text()
    finally:
        store.close()


def test_corrupt_export_does_not_publish_partial_output(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        record = store.create('test', 'system')
        (store.directory(record['id']) / 'messages.jsonl').write_text('broken')
        with pytest.raises(ValueError):
            store.export_saved(record['id'])
        assert not list((store.root / 'exports').glob('*'))
    finally:
        store.close()
