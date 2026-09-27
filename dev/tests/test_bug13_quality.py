
def test_quality_metrics_deduplicate_parent_hits():
    from dev.rag.quality import metrics
    rows = [{'id': 'q', 'relevant': ['a'], 'ranking': [('b', 3), ('b', 2), ('a', 1)]}]
    result = metrics(rows, threshold=0, k=2)
    assert result['hit@k'] == 1
    assert result['MRR'] == .5


def test_threshold_selection_uses_calibration_split_only():
    from dev.rag.quality import calibrate
    rows = [{'id': 'cal', 'split': 'calibration', 'relevant': ['a'], 'ranking': [('a', 2), ('b', -2)]},
            {'id': 'eval', 'split': 'evaluation', 'relevant': ['b'], 'ranking': [('b', 1000)]}]
    assert calibrate(rows) == calibrate(rows[:1])


def test_frozen_public_quality_splits_are_disjoint():
    import json
    from pathlib import Path
    manifest = json.loads(Path('dev/rag/inputs/quality.json').read_text())
    calibration = {row['id'] for row in manifest['queries'] if row['split'] == 'calibration'}
    evaluation = {row['id'] for row in manifest['queries'] if row['split'] == 'evaluation'}
    assert len(calibration) == len(evaluation) == 5
    assert not calibration & evaluation
    assert all(set(row['relevant']) <= set(manifest['document_ids']) for row in manifest['queries'])


def test_real_quality_report_optional(tmp_path):
    import os
    import subprocess
    import sys
    import json
    import pytest
    corpus = os.getenv('RAG_QUALITY_CORPUS')
    if not corpus:
        pytest.skip('设置 RAG_QUALITY_CORPUS 和本地模型路径后运行公开标注质量回归')
    output = tmp_path / 'quality'
    subprocess.run([sys.executable, '-m', 'dev.rag.quality', '--corpus', corpus, '--output', str(output)], check=True)
    summary = json.loads((output / 'summary.json').read_text())
    assert summary['queries'] == 10
    assert summary['evaluation_default']['hit@k'] >= .6
    assert summary['evaluation_default']['MRR'] >= .5


import pytest
test_real_quality_report_optional = pytest.mark.integration(test_real_quality_report_optional)


def test_configuration_example_keeps_all_runtime_settings():
    import ast
    from pathlib import Path
    from dotenv import dotenv_values
    tree = ast.parse(Path('config.py').read_text())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
            if name in {'env_int', 'env_float', 'env_bool', 'env_path', 'getenv'} and isinstance(node.args[0].value, str):
                keys.add(node.args[0].value)
    assert keys <= dotenv_values('.env.example').keys()
