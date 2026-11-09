import asyncio
import json
import threading

from agent_core.core.storage import SessionStore


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


def test_session_writer_lanes_keep_same_session_order_and_let_others_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(SessionStore, '_SESSION_SAVE_LANES', 3, raising=False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    first = store.new_record('first', 'system')
    first['id'] = '0' * 32
    others = [store.new_record(f'other-{lane}', 'system') for lane in (1, 2)]
    for lane, record in enumerate(others, start=1):
        record['id'] = f'{lane:032x}'
    write = store.save
    entered, release = threading.Event(), threading.Event()

    def hold_first(snapshot):
        if snapshot['id'] == first['id'] and snapshot['_count'] == 2:
            entered.set()
            assert release.wait(10)
        write(snapshot)

    monkeypatch.setattr(store, 'save', hold_first)

    async def exercise():
        first['messages'].append({'role': 'user', 'content': 'first input'})
        initial = store.save_async(first)
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            first['messages'].append({'role': 'assistant', 'content': 'first reply'})
            next_save = store.save_async(first)
            for record in others:
                record['messages'].append({'role': 'user', 'content': record['title']})
            separate = asyncio.gather(*(store.save_async(record) for record in others))
            await asyncio.wait_for(asyncio.shield(separate), 3)
            for record in others:
                saved = store.read_record(store.directory(record['id']) / 'session.json')
                assert saved['messages'] == record['messages']
            assert not next_save.done()
        finally:
            release.set()
        await asyncio.gather(initial, next_save)
        assert store.read_record(store.directory(first['id']) / 'session.json')['messages'] == first['messages']

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        store.close()


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
    from agent_core import config
    from agent_test.scripts import diagnose
    from agent_core.core.sessions import SessionManager
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
