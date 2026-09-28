from ai_agent_startup import config
from ai_agent_startup.core.storage import SessionStore


def test_memory_deduplicates_and_removes_stable_entries(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        record = store.create('m', 's')
        store.remember(record['id'], '偏好中文')
        store.remember(record['id'], '偏好中文')
        assert store.memory(record['id']).count('偏好中文') == 1
        entries = store.memory_entries(record['id'])
        store.remove_memory(record['id'], entries[0]['id'])
        assert not store.memory_entries(record['id'])
    finally:
        store.close()


def test_relevant_memory_is_selected_with_injection_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MEMORY_TOP_K', 1, raising=False)
    monkeypatch.setattr(config, 'MEMORY_INJECT_CHARS', 80, raising=False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        record = store.create('m', 's')
        store.remember(record['id'], '咖啡 喜欢拿铁')
        store.remember(record['id'], 'Python 项目使用 pytest')
        selected = store.memory_for_model(record['id'], 'Python')
        assert 'pytest' in selected and '咖啡' not in selected
        assert len(selected) <= 80
    finally:
        store.close()
