from pathlib import Path


def test_ci_runs_locked_offline_suite_and_probes_real_sandbox():
    path = Path('.github/workflows/tests.yml')
    assert path.is_file(), '缺少默认 PR 回归工作流'
    text = path.read_text()
    assert 'pull_request:' in text
    assert 'uv sync --locked' in text
    assert "pytest -m 'not integration' -q" in text
    assert 'dev/ci_sandbox_probe.py' in text
    assert 'continue-on-error' not in text
    assert 'workflow_dispatch:' in text and 'actions/cache@' in text


def test_runner_context_is_not_used_in_job_env():
    text = Path('.github/workflows/tests.yml').read_text()
    assert '${{ runner.' not in text.split('steps:', 1)[0]


def test_release_workflow_points_at_docker_directory():
    """Dockerfile 位于 docker/ 子目录，构建步骤必须显式指定 file。"""
    text = Path('.github/workflows/release.yml').read_text()
    assert 'file: docker/Dockerfile' in text
    assert 'context: .' in text
