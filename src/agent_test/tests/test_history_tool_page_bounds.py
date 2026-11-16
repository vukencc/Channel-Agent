"""历史工具的小页是真实、完整的 JSON；原始归档及检索次序不变。"""

import asyncio
import copy
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.history_context import HistoryContext
from agent_core.core.session_service import session_service_context
from agent_core.core.storage import SessionStore
from agent_core.tools import context_history


def _rows(count=12, summary='中文摘要\\\"\n' * 1200):
    return [{'ID': f'{index:032x}', 'Time': {'start': index, 'end': index + 1},
             'Summary': summary, 'score': 12.0 - index,
             'retrieval': {'effective_method': 'hybrid', 'rerank_score': 12.0 - index},
             'previous_id': f'{index - 1:032x}' if index else None,
             'next_id': f'{index + 1:032x}' if index + 1 < count else None,
             'RawHistory': '不得泄漏原文'} for index in range(count)]


@pytest.mark.parametrize('summary', ['中文资料' * 1800, '\\"\n\t' * 1800], ids=['chinese', 'escaped'])
def test_search_caps_two_real_large_receipts_without_mutating_input(monkeypatch, summary):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 12000)
    rows = _rows(summary=summary)
    original = copy.deepcopy(rows)
    payloads = [context_history._search_json(rows) for _ in range(2)]
    assert all(len(payload) <= 1500 for payload in payloads)
    for payload in payloads:
        selected = json.loads(payload)
        assert selected
        assert selected[0]['omitted_count'] == len(rows) - len(selected) > 0
        assert '仅摘要' in selected[0]['read_hint']
        assert 'history_read' in selected[0]['read_hint']
        assert 'limit' in selected[0]['read_hint']
        for actual, source in zip(selected, rows):
            for key in ('ID', 'Time', 'score', 'retrieval', 'previous_id', 'next_id'):
                assert actual[key] == source[key]
            assert actual['summary_truncated'] is True
            assert source['Summary'].startswith(actual['Summary'])
            assert 'RawHistory' not in actual
    assert rows == original


def test_search_shortens_each_summary_before_dropping_ranked_hits(monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 12000)
    rows = _rows(count=2)
    selected = json.loads(context_history._search_json(rows))
    assert [row['ID'] for row in selected] == [row['ID'] for row in rows]
    assert all(row['summary_truncated'] for row in selected)
    assert 'omitted_count' not in selected[0]


@pytest.mark.parametrize('maximum', [230, 300, 1500])
def test_search_respects_smaller_output_budget_and_true_candidate_count(monkeypatch, maximum):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', maximum)
    rows = [{'ID': f'{index:032x}', 'Summary': '正文\\\"' * 1000,
             'score': 1.0} for index in range(3)]
    payload = context_history._search_json(rows)
    result = json.loads(payload)
    assert len(payload) <= min(maximum, 1500)
    assert result[0].get('omitted_count', 0) == len(rows) - len(result)
    assert all('total_hits' not in row for row in result)
    assert result[0]['summary_truncated'] is True


def test_search_metadata_that_cannot_fit_fails_without_mutation(monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 300)
    rows = [{'ID': 'a' * 32, 'Summary': '小摘要', 'retrieval': {'detail': 'M' * 2000}}]
    original = copy.deepcopy(rows)
    with pytest.raises(ValueError, match='历史|元数据'):
        context_history._search_json(rows)
    assert rows == original


def test_search_empty_and_small_results_remain_real_compatible_lists(monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 1500)
    assert context_history._search_json([]) == '[]'
    rows = [{'ID': 'a' * 32, 'Summary': '原始短摘要', 'score': 1.0}]
    assert json.loads(context_history._search_json(rows)) == rows


def test_empty_search_still_rejects_an_impossibly_small_json_budget(monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 1)
    with pytest.raises(ValueError, match='历史'):
        context_history._search_json([])


def test_search_audit_counts_real_candidates_and_keeps_current_owner(monkeypatch):
    async def run():
        owner = SimpleNamespace(id='owner')
        rows, seen, audits = _rows(), [], []
        original = copy.deepcopy(rows)

        class History:
            async def search(self, session, query, **kwargs):
                seen.append((session, query, kwargs))
                return rows

        manager = SimpleNamespace(history_context=History(), sessions={'owner': owner}, closing=False)
        monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 12000)
        monkeypatch.setattr(context_history, 'audit', lambda *a, **kw: audits.append((a, kw)))
        with session_service_context(manager, 'owner', asyncio.get_running_loop()):
            payload = await asyncio.to_thread(context_history.history_search, '  needle  ',
                                              limit=12, method='hybrid')
        selected = json.loads(payload)
        assert len(payload) <= 1500
        assert selected[0]['omitted_count'] == len(rows) - len(selected)
        assert seen == [(owner, 'needle', {'limit': 12, 'method': 'hybrid'})]
        assert audits == [(('read',), {'action': 'history_search', 'count': len(rows), 'method': 'hybrid'})]
        assert rows == original

    asyncio.run(run())


def test_complete_json_uses_the_history_page_cap(monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 12000)
    with pytest.raises(ValueError, match='历史'):
        context_history._complete_json({'content': '中' * 1500})


@pytest.mark.parametrize('maximum', [350, 12000])
def test_read_passes_page_cap_to_producer_preserving_owner_and_audit(monkeypatch, maximum):
    async def run():
        owner = SimpleNamespace(id='owner')
        seen, audits = [], []
        result = {'chunks': [{'ID': 'a' * 32, 'RawHistory': '真实原文', 'chunk_offset': 7}],
                  'next_offset': 11, 'total_chars': 25}

        class History:
            async def read(self, session, chunk_id, **kwargs):
                seen.append((session, chunk_id, kwargs))
                return result

        manager = SimpleNamespace(history_context=History(), sessions={'owner': owner}, closing=False)
        monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', maximum)
        monkeypatch.setattr(context_history, 'audit', lambda *a, **kw: audits.append((a, kw)))
        with session_service_context(manager, 'owner', asyncio.get_running_loop()):
            payload = await asyncio.to_thread(context_history.history_read, 'a' * 32,
                                              offset=7, limit=3000)
        assert json.loads(payload) == result
        assert len(payload) <= min(maximum, 1500)
        assert seen[0][0] is owner
        assert seen[0][1] == 'a' * 32
        assert seen[0][2]['output_limit'] == min(maximum, 1500)
        assert seen[0][2]['offset'] == 7
        assert audits == [(('read',), {'action': 'history_read', 'chunk_id': 'a' * 32,
                                     'before': 0, 'after': 0, 'offset': 7})]

    asyncio.run(run())


@pytest.fixture
def archive(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspaces')
    session = SimpleNamespace(id='a' * 32, cancelled=threading.Event(), deleting=False, forking=False)
    manager = SimpleNamespace(store=store, sessions={session.id: session}, closing=False,
                              maintenance=False, pending_saves=set())
    service = HistoryContext(manager, supervisor=object())
    manager.history_context = service
    messages = [{'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'receipt', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'receipt', 'content': '中文\\\"\n\t' * 1300}]
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    record = {'ID': 'b' * 32, 'Time': {'start': 2, 'end': 4, 'created_at': '2026-01-01'},
              'Summary': '真实归档摘要', 'RawHistory': raw, 'digest': digest,
              'session_id': session.id, 'summary_kind': 'extractive_budget'}
    manifest = {'version': 1, 'owner_id': session.id, 'revision': 1, 'session_summary': '',
                'summary_kind': 'none', 'updated_at': '',
                'chunks': [{k: v for k, v in record.items() if k not in {'RawHistory', 'session_id'}}]}
    service._publish(session, [record], manifest)
    path = store.directory(session.id) / 'context' / 'chunks' / (record['ID'] + '.json')
    original = path.read_bytes()
    yield service, session, manager, record, path, original
    asyncio.run(service.close())
    store.close()


@pytest.mark.parametrize('maximum', [350, 1500])
def test_direct_read_pages_reconstruct_exact_archive_with_truthful_offsets(archive, maximum, monkeypatch):
    async def run():
        service, session, _, record, path, original = archive
        monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 12000)
        offset, pieces = 0, []
        while True:
            page = await service.read(session, record['ID'], offset=offset, limit=6000,
                                      output_limit=maximum)
            assert len(json.dumps(page, ensure_ascii=False)) <= maximum
            assert page['total_chars'] == len(record['RawHistory'])
            part = ''.join(chunk['RawHistory'] for chunk in page['chunks'])
            assert part
            assert page['chunks'][0]['chunk_offset'] == offset
            assert part == record['RawHistory'][offset:offset + len(part)]
            pieces.append(part)
            if page['next_offset'] is None:
                assert offset + len(part) == len(record['RawHistory'])
                break
            assert page['next_offset'] == offset + len(part) > offset
            offset = page['next_offset']
        assert ''.join(pieces) == record['RawHistory']
        assert path.read_bytes() == original

    asyncio.run(run())


def test_direct_read_fails_if_real_metadata_cannot_fit(archive, monkeypatch):
    async def run():
        service, session, _, record, path, original = archive
        monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 12000)
        with pytest.raises(ValueError, match='元数据'):
            await service.read(session, record['ID'], output_limit=20)
        assert path.read_bytes() == original

    asyncio.run(run())


def test_real_read_tool_page_and_permission_cancellation_boundaries(archive, monkeypatch):
    async def run():
        service, session, manager, record, path, original = archive
        audits = []
        monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 12000)
        monkeypatch.setattr(context_history, 'audit', lambda *a, **kw: audits.append((a, kw)))
        with session_service_context(manager, session.id, asyncio.get_running_loop()):
            payload = await asyncio.to_thread(context_history.history_read, record['ID'])
        page = json.loads(payload)
        assert len(payload) <= 1500
        assert page['next_offset'] == sum(len(chunk['RawHistory']) for chunk in page['chunks'])
        assert len(audits) == 1
        with session_service_context(manager, 'other', asyncio.get_running_loop()):
            with pytest.raises(PermissionError):
                await asyncio.to_thread(context_history.history_read, record['ID'])
        monkeypatch.setattr(context_history, 'cancellation_requested', lambda: True)
        with session_service_context(manager, session.id, asyncio.get_running_loop()):
            with pytest.raises(InterruptedError):
                await asyncio.to_thread(context_history.history_read, record['ID'])
        assert len(audits) == 1
        assert path.read_bytes() == original

    asyncio.run(run())
