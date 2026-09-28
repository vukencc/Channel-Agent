import json

from tools import sandbox


def test_audit_reuses_append_handle(sandbox_env, monkeypatch):
    import builtins
    calls = []
    original = builtins.open
    def opened(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(builtins, 'open', opened)
    for i in range(20):
        sandbox.audit('test', index=i)
    assert len(calls) <= 1
    lines = sandbox.config.AUDIT_LOG.read_text().splitlines()
    assert [json.loads(line)['index'] for line in lines] == list(range(20))


def test_rotation_and_sync(sandbox_env, monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox.config, 'AUDIT_SYNC', True, raising=False)
    monkeypatch.setattr(sandbox.os, 'fsync', lambda descriptor: calls.append(descriptor))
    sandbox.audit('before')
    path = sandbox.config.AUDIT_LOG
    path.rename(path.with_suffix('.old'))
    sandbox.audit('after')
    assert json.loads(path.read_text())['event'] == 'after'
    assert len(calls) == 2


def test_audit_handle_cache_is_bounded_and_lines_complete(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from core.audit_writer import append_audit, close_audit_handles, _handles
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: append_audit(tmp_path / 'shared.jsonl', json.dumps({'i': i})), range(100)))
        assert sorted(json.loads(line)['i'] for line in (tmp_path / 'shared.jsonl').read_text().splitlines()) == list(range(100))
        for i in range(70):
            append_audit(tmp_path / f'{i}.jsonl', '{}')
        assert len(_handles) <= 64
    finally:
        close_audit_handles()
