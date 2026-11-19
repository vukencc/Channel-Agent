"""验证会话日志的追加位置及索引发布顺序。"""
import json
import os

import pytest

from agent_core.core import storage


class TrackedStream:
    def __init__(self, stream, events, fail_write=False):
        self.stream = stream
        self.events = events
        self.fail_write = fail_write

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def seek(self, *args):
        self.events.append(('seek', args))
        return self.stream.seek(*args)

    def truncate(self, *args):
        self.events.append(('truncate', args))
        return self.stream.truncate(*args)

    def write(self, data):
        self.events.append(('write',))
        if self.fail_write:
            raise OSError('journal write failed')
        return self.stream.write(data)

    def flush(self):
        self.events.append(('flush',))
        return self.stream.flush()

    def fileno(self):
        return self.stream.fileno()

    def tell(self):
        return self.stream.tell()


def track_commit(monkeypatch, store, events, fail_write=False):
    fdopen = os.fdopen
    fsync = os.fsync
    atomic_write = store.atomic_write
    def tracked_fdopen(fd, mode, *args, **kwargs):
        stream = fdopen(fd, mode, *args, **kwargs)
        return TrackedStream(stream, events, fail_write) if mode == 'r+b' else stream

    monkeypatch.setattr(storage.os, 'fdopen', tracked_fdopen)

    def tracked_fsync(fd):
        events.append(('fsync',))
        return fsync(fd)

    def tracked_atomic(path, text):
        events.append(('atomic',))
        return atomic_write(path, text)

    monkeypatch.setattr(storage.os, 'fsync', tracked_fsync)
    monkeypatch.setattr(store, 'atomic_write', tracked_atomic)


@pytest.mark.parametrize('append', [False, True])
def test_committed_eof_needs_no_truncate_or_second_seek(tmp_path, monkeypatch, append):
    store = storage.SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        record = store.new_record('title', 'system')
        store.save(record)
        if append:
            record['messages'].append({'role': 'user', 'content': 'next'})
        events = []
        track_commit(monkeypatch, store, events)
        store.save(record)
        assert [event for event in events if event[0] in ('seek', 'truncate')] == [('seek', (0, os.SEEK_END))]
        assert [event[0] for event in events if event[0] in ('flush', 'fsync', 'atomic')][:3] == ['flush', 'fsync', 'atomic']
        assert store.read_record(store.directory(record['id']) / 'session.json')['messages'] == record['messages']
    finally:
        store.close()


def test_crash_tail_is_truncated_before_append(tmp_path, monkeypatch):
    store = storage.SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        record = store.new_record('title', 'system')
        store.save(record)
        directory = store.directory(record['id'])
        offset = json.loads((directory / 'session.json').read_text(encoding='utf-8'))['message_bytes']
        with (directory / 'messages.jsonl').open('ab') as stream:
            stream.write(b'crash tail')
        record['messages'].append({'role': 'user', 'content': 'next'})
        events = []
        track_commit(monkeypatch, store, events)
        store.save(record)
        assert [event for event in events if event[0] in ('seek', 'truncate')] == [
            ('seek', (0, os.SEEK_END)), ('truncate', (offset,)), ('seek', (offset,))]
        assert [event[0] for event in events if event[0] in ('flush', 'fsync', 'atomic')][:3] == ['flush', 'fsync', 'atomic']
        assert store.read_record(directory / 'session.json')['messages'] == record['messages']
    finally:
        store.close()


@pytest.mark.parametrize('failure', ['short', 'write'])
def test_failed_journal_does_not_publish_index(tmp_path, monkeypatch, failure):
    store = storage.SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        record = store.new_record('title', 'system')
        store.save(record)
        directory = store.directory(record['id'])
        index = directory / 'session.json'
        previous = index.read_bytes()
        if failure == 'short':
            with (directory / 'messages.jsonl').open('r+b') as stream:
                stream.truncate(0)
        record['messages'].append({'role': 'user', 'content': 'next'})
        events = []
        track_commit(monkeypatch, store, events, fail_write=failure == 'write')
        with pytest.raises((ValueError, OSError)):
            store.save(record)
        assert index.read_bytes() == previous
        assert not any(event[0] == 'atomic' for event in events)
    finally:
        store.close()
