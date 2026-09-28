import json
import os
import subprocess


def test_config_changes_prompt_schema_and_validation_together():
    result = subprocess.run(['.venv/bin/python', '-c', '''
import json
from core.prompts import DEFAULT_PROMPT, FILE_WORKFLOW_GUIDE
from tools.file_crud import AppendFileArgs, ReadFileArgs
print(json.dumps({'prompt': DEFAULT_PROMPT, 'guide': FILE_WORKFLOW_GUIDE,
                 'append': AppendFileArgs.model_json_schema()['properties']['content'],
                 'page': ReadFileArgs.model_json_schema()['properties']['limit']}))
'''], env={**os.environ, 'FILE_APPEND_CHARS': '80', 'RAG_RETRY_LIMIT': '3', 'FILE_READ_CHARS': '90'},
        capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    assert '80 字符' in data['guide']
    assert '重试 3 次' in data['prompt']
    assert data['append']['maxLength'] == 80
    assert data['page']['default'] == 90


def test_custom_prompt_file_is_explicit_and_missing_file_fails(tmp_path):
    path = tmp_path / 'prompt.txt'
    path.write_text('custom system prompt')
    environment = {**os.environ, 'SYSTEM_PROMPT_FILE': str(path)}
    command = ['.venv/bin/python', '-c', 'from core.prompts import DEFAULT_PROMPT; print(DEFAULT_PROMPT)']
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == 'custom system prompt'
    path.unlink()
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert result.returncode != 0


def test_default_prompt_text_matches_pre_template_snapshot():
    result = subprocess.run(['.venv/bin/python', '-c', '''
import hashlib
from core.prompts import DEFAULT_PROMPT, FILE_WORKFLOW_GUIDE
for text in (DEFAULT_PROMPT, FILE_WORKFLOW_GUIDE):
    print(hashlib.sha256(text.encode()).hexdigest())
'''], env={**os.environ, 'SYSTEM_PROMPT_FILE': '', 'FILE_APPEND_CHARS': '4000',
           'RAG_RETRY_LIMIT': '2'}, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [
        '85f2f94d2eceeea9140275acf255872eedf17f8cf3ab8648711079b6aabb89cf',
        'b4a57d3cf49c43fc8e2887de26d4845bfdc4781420c2b821b1ccf869246058b5']
