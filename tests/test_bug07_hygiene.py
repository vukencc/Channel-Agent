import ast
from pathlib import Path


def test_legacy_module_has_no_second_agent_loop():
    tree = ast.parse(Path('src/ai_agent_startup/core/agent.py').read_text())
    assert not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in tree.body)


def test_contributor_test_paths_match_repository():
    text = Path('AGENTS.md').read_text()
    assert 'tests/test_command.py' in text
