import importlib
import json
import os
import subprocess
import sys

from ai_agent_startup import config


def test_default_model_schema_excludes_debug():
    code = 'import json; from ai_agent_startup.tools import all_schemas; print(json.dumps([s["function"]["name"] for s in all_schemas()]))'
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'ENABLE_DEBUG_TOOL': '0'}, capture_output=True, text=True, check=True)
    assert 'tool_debug' not in json.loads(result.stdout)


def test_rag_debug_never_prints(monkeypatch, capsys):
    module = importlib.import_module('ai_agent_startup.tools.rag_search')
    monkeypatch.setattr(config, 'DEBUG', True)
    monkeypatch.setattr(module, '_rag_search', lambda *a, **kw: 'retrieved content')
    assert module.rag_search('query') == 'retrieved content'
    assert capsys.readouterr().out == ''
