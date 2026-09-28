import asyncio
import json

from ai_agent_startup.core.storage import SessionStore


def test_large_history_save_writes_only_new_message(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        record = store.new_record('long', 'system')
        record['messages'] += [{'role': 'user', 'content': str(i) + 'x' * 1000} for i in range(10000)]
        store.save(record)
        directory = store.directory(record['id'])
        assert (directory / 'session.json').stat().st_size < 5000
        before = (directory / 'messages.jsonl').stat().st_size
        record['messages'].append({'role': 'assistant', 'content': 'tail'})
        asyncio.run(save(store, record))
        assert (directory / 'messages.jsonl').stat().st_size - before < 100
        assert store.load_all()[0]['messages'] == record['messages']
    finally:
        store.close()


async def save(store, record):
    await store.save_async(record)


def test_uncommitted_journal_tail_is_not_replayed_and_v1_backup_preserved(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        record = store.new_record('legacy', 'system')
        directory = store.directory(record['id'])
        directory.mkdir()
        original = json.dumps(record)
        (directory / 'session.json').write_text(original)
        store.save(record)
        assert (directory / 'session.v1.bak').read_text() == original
        with (directory / 'messages.jsonl').open('ab') as stream:
            stream.write(b'{"partial":')
        assert store.load_all()[0]['messages'] == record['messages']
    finally:
        store.close()


def test_diagnostic_replay_accepts_v2_without_modifying_source(tmp_path, monkeypatch):
    from ai_agent_startup import config
    from dev import diagnose
    from ai_agent_startup.core.sessions import SessionManager
    monkeypatch.setattr(config, 'SANDBOX_DIR', tmp_path / 'work')
    for name in ('REASONING_EFFORT', 'THINKING_MODE', 'RAG_ASSESS'):
        monkeypatch.setattr(config, name, getattr(config, name))
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    record = store.create('source', 's')
    record['messages'].append({'role': 'user', 'content': 'replay me'})
    store.save(record)
    store.workspace(record['id'])
    path = store.directory(record['id']) / 'session.json'
    before = path.read_bytes()
    store.close()
    async def model(history, **kwargs):
        assert history[-1]['content'] == 'replay me'
        return {'role': 'assistant', 'content': 'done'}
    monkeypatch.setattr(diagnose, 'SessionManager', lambda store: SessionManager(store, model=model))
    output = tmp_path / 'output'
    output.mkdir()
    rows = asyncio.run(diagnose.run_live(output, 1, 'low', 'disabled', path))
    assert rows[0]['status'] == 'idle'
    assert path.read_bytes() == before
