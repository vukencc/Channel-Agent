from core.sessions import SessionManager
from core.storage import SessionStore


def test_manager_reads_only_selected_history(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        records = [store.create(str(i), 'system') for i in range(3)]
        calls = []
        original = store.read_record
        monkeypatch.setattr(store, 'read_record', lambda path: (calls.append(path), original(path))[1])
        manager = SessionManager(store)
        assert calls == []
        assert manager.sessions[records[1]['id']].record['messages'] == records[1]['messages']
        assert len(calls) == 1
        assert manager.sessions[records[1]['id']].record['messages'] == records[1]['messages']
        assert len(calls) == 1
    finally:
        store.close()


def test_lazy_recovery_pairs_pending_tools(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        record = store.create('interrupted', 'system')
        record['status'] = 'running'
        record['messages'].append({'role': 'assistant', 'tool_calls': [
            {'id': 'pending', 'function': {'name': 'create_file', 'arguments': '{}'}}]})
        store.save(record)
        manager = SessionManager(store)
        restored = manager.sessions[record['id']].record
        assert restored['messages'][-1]['tool_call_id'] == 'pending'
        assert restored['status'] == 'interrupted'
    finally:
        store.close()


def test_metadata_is_read_only_and_rename_preserves_messages(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        record = store.create('old', 'system')
        record['messages'].append({'role': 'user', 'content': 'keep'})
        record['status'] = 'running'
        store.save(record)
        path = store.directory(record['id']) / 'session.json'
        before = path.read_bytes()
        metadata = store.list_metadata()[0]
        assert path.read_bytes() == before
        metadata['title'] = 'new'
        store.save(metadata)
        restored = store.read_record(path)
        assert restored['title'] == 'new'
        assert restored['messages'] == record['messages']
    finally:
        store.close()
