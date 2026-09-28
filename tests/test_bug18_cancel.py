import threading

import pytest

from ai_agent_startup.tools.sandbox import ToolContext, tool_context
from ai_agent_startup.rag.tool import rag_search


def test_cancelled_retrieval_does_not_start_expensive_index(tmp_path, monkeypatch):
    from ai_agent_startup import rag
    called = []
    def index():
        called.append(True)
        raise RuntimeError('不应进入索引')
    monkeypatch.setattr(rag.tool, 'get_index', index)
    event = threading.Event()
    event.set()
    with tool_context(ToolContext(tmp_path, tmp_path / 'audit', lambda *_: True, event)):
        with pytest.raises(InterruptedError):
            rag_search('query')
    assert not called
