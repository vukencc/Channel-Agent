from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from agent_core import config
from agent_core.rag import rerank
from agent_core.tools.sandbox import ToolContext, tool_context


@pytest.fixture(autouse=True)
def isolated_reranker(tmp_path, monkeypatch):
    rerank._get_reranker.cache_clear()
    monkeypatch.setattr(config, 'RERANK_LOCAL_PATH', tmp_path)
    yield
    rerank._get_reranker.cache_clear()


def test_same_reranker_key_constructs_once(monkeypatch):
    lookup_ready = Barrier(3, timeout=5)
    start, release, duplicate = Event(), Event(), Event()
    instances = []
    instances_lock = Lock()
    local_path = rerank.local_reranker_path

    def simultaneous_lookup():
        lookup_ready.wait()
        return local_path()

    class OfflineReranker:
        def __init__(self):
            with instances_lock:
                instances.append(self)
                if len(instances) > 1:
                    duplicate.set()
            start.set()
            assert release.wait(5)

    monkeypatch.setattr(rerank, 'local_reranker_path', simultaneous_lookup)
    monkeypatch.setattr(rerank, 'Reranker', OfflineReranker)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(rerank.get_reranker) for _ in range(3)]
        try:
            assert start.wait(5)
            assert not duplicate.wait(.25)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(instances) == 1
    assert all(result is instances[0] for result in results)


def test_cancelled_reranker_waiter_exits_without_discarding_loaded_model(monkeypatch, sandbox_env):
    start, release, waiter_lookup = Event(), Event(), Event()
    leader_cancelled, waiter_cancelled, waiter_done = Event(), Event(), Event()
    instances = []
    local_path = rerank.local_reranker_path

    class OfflineReranker:
        def __init__(self):
            start.set()
            assert release.wait(5)
            instances.append(self)

    def signal_waiter_lookup():
        waiter_lookup.set()
        return local_path()

    def load(cancelled: Event, done: Event | None = None) -> object:
        context = ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True, cancelled)
        try:
            with tool_context(context):
                return rerank.get_reranker()
        finally:
            if done is not None:
                done.set()

    monkeypatch.setattr(rerank, 'Reranker', OfflineReranker)
    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(load, leader_cancelled)
        try:
            assert start.wait(5)
            monkeypatch.setattr(rerank, 'local_reranker_path', signal_waiter_lookup)
            waiter = executor.submit(load, waiter_cancelled, waiter_done)
            assert waiter_lookup.wait(5)
            assert not waiter_done.wait(.15)
            waiter_cancelled.set()
            with pytest.raises(InterruptedError, match='检索已取消'):
                waiter.result(timeout=2)
        finally:
            leader_cancelled.set()
            release.set()
        with pytest.raises(InterruptedError, match='检索已取消'):
            leader.result(timeout=5)

    assert len(instances) == 1
    assert rerank.get_reranker() is instances[0]


def test_reranker_retries_after_failed_construction(monkeypatch):
    attempts = 0

    def construct() -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('offline construction failed')
        return object()

    monkeypatch.setattr(rerank, 'Reranker', construct)
    with pytest.raises(RuntimeError, match='offline construction failed'):
        rerank.get_reranker()
    assert rerank._get_reranker.cache_info().currsize == 0
    recovered = rerank.get_reranker()
    assert rerank.get_reranker() is recovered
    assert attempts == 2


def test_reranker_preserves_model_and_artifact_cache_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(rerank, 'Reranker', object)
    artifact = tmp_path / 'model.safetensors'
    artifact.write_bytes(b'first')
    original = rerank.get_reranker()
    assert rerank.get_reranker() is original

    monkeypatch.setattr(config, 'RERANK_MODEL', 'offline/replacement')
    replacement = rerank.get_reranker()
    assert replacement is not original
    assert rerank.get_reranker() is replacement

    artifact.write_bytes(b'updated model')
    updated = rerank.get_reranker()
    assert updated is not replacement
    assert rerank.get_reranker() is updated
    assert rerank._get_reranker.cache_info().currsize == 1
