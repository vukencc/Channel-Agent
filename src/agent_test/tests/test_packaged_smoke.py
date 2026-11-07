"""打包验收编排的离线回归；此处不执行模型或本地依赖探测。"""
import json
import threading

import pytest

from agent_test.scripts import packaged_smoke as smoke


def test_success_reports_only_capabilities(tmp_path, monkeypatch):
    seen = []
    def check(root):
        seen.append(root)
        assert root.is_dir()
    monkeypatch.setattr(smoke, '_capabilities', lambda: {'controlled_fixture': check})
    report = tmp_path / 'report.json'
    assert smoke.run(report) == 0
    payload = json.loads(report.read_text(encoding='utf-8'))
    assert payload == {'schema_version': 1, 'ok': True,
                       'capabilities': {'controlled_fixture': True}, 'errors': {}}
    assert len(seen) == 1 and not seen[0].exists()


def test_failure_redacts_messages_and_continues(tmp_path, monkeypatch):
    def fail(root):
        raise RuntimeError('sk-secret https://user:password@private.invalid conversation')
    monkeypatch.setattr(smoke, '_capabilities', lambda: {'broken': fail, 'later': lambda root: None})
    report = tmp_path / 'failure.json'
    assert smoke.run(report) == 1
    payload = json.loads(report.read_text(encoding='utf-8'))
    assert payload['capabilities'] == {'broken': False, 'later': True}
    assert payload['errors'] == {'broken': 'RuntimeError'}
    assert not payload['ok']
    assert 'secret' not in report.read_text(encoding='utf-8')
    assert 'password' not in report.read_text(encoding='utf-8')


def test_custom_exception_class_is_redacted(tmp_path, monkeypatch):
    exception = type('sk_private_credential', (Exception,), {})
    def fail(root):
        raise exception('private')
    monkeypatch.setattr(smoke, '_capabilities', lambda: {'failure': fail})
    report = tmp_path / 'failure.json'
    assert smoke.run(report) == 1
    assert json.loads(report.read_text())['errors'] == {'failure': 'Exception'}


def test_cancelled_check_stops_and_cleans_temp(tmp_path, monkeypatch):
    roots = []
    def cancel(root):
        roots.append(root)
        raise KeyboardInterrupt('private')
    def later(root):
        pytest.fail('取消后不能继续能力探测')
    monkeypatch.setattr(smoke, '_capabilities', lambda: {'cancelled': cancel, 'later': later})
    report = tmp_path / 'cancelled.json'
    assert smoke.run(report) == 130
    payload = json.loads(report.read_text())
    assert not payload['ok']
    assert payload['errors'] == {'cancelled': 'KeyboardInterrupt'}
    assert payload['capabilities'] == {'cancelled': False}
    assert not roots[0].exists()


def test_report_write_failure_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, '_capabilities', lambda: {})
    assert smoke.run(tmp_path) == 1


def test_wait_timeout_yields_and_is_bounded(monkeypatch):
    ticks = iter([0, 0, .5, 1.1])
    sleeps = []
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(smoke.time, 'sleep', sleeps.append)
    class App:
        def processEvents(self):
            pass
    with pytest.raises(TimeoutError):
        smoke._wait_until(App(), lambda: False, timeout=1)
    assert sleeps == [.01, .01]


def test_wait_cancelled_before_event_processing():
    cancelled = threading.Event()
    cancelled.set()
    class App:
        def processEvents(self):
            pytest.fail('取消后不应处理事件')
    with pytest.raises(InterruptedError):
        smoke._wait_until(App(), lambda: False, cancelled=cancelled)


def test_real_file_tools_and_selected_permission(sandbox_env, tmp_path):
    smoke._check_file_tools(tmp_path)
    assert not (tmp_path / 'files' / 'denied.txt').exists()
    assert (tmp_path / 'files' / 'probe.txt').read_text(encoding='utf-8') == '打包文件验收'
    entries = [json.loads(line) for line in (tmp_path / 'file-audit.jsonl').read_text(encoding='utf-8').splitlines()]
    assert entries


def test_real_flow_template_persists_without_model(tmp_path):
    smoke._check_flow_template(tmp_path)


def test_wsl_probe_opt_in(monkeypatch):
    monkeypatch.delenv('CHANNEL_AGENT_SELF_CHECK_WSL', raising=False)
    assert 'wsl_command' not in smoke._capabilities()
    monkeypatch.setenv('CHANNEL_AGENT_SELF_CHECK_WSL', '1')
    assert 'wsl_command' in smoke._capabilities()


def test_config_root_is_stable_and_cwd_restored(tmp_path, monkeypatch):
    from agent_core import config
    selected = tmp_path / 'selected-state'
    monkeypatch.setenv('AI_AGENT_PROJECT_ROOT', str(selected))
    monkeypatch.setattr(config, 'PROJECT_ROOT', selected)
    original = smoke.Path.cwd()
    smoke._check_config_root(tmp_path)
    assert smoke.Path.cwd() == original


def test_mismatched_explicit_root_fails(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setenv('AI_AGENT_PROJECT_ROOT', str(tmp_path / 'selected-state'))
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path / 'wrong-state')
    with pytest.raises(RuntimeError):
        smoke._check_config_root(tmp_path)


def test_offline_environment_restored_on_failure(monkeypatch):
    monkeypatch.setenv('HF_HUB_OFFLINE', 'previous')
    monkeypatch.delenv('TRANSFORMERS_OFFLINE', raising=False)
    with pytest.raises(RuntimeError):
        with smoke._offline_environment():
            assert smoke.os.environ['HF_HUB_OFFLINE'] == '1'
            assert smoke.os.environ['TRANSFORMERS_OFFLINE'] == '1'
            raise RuntimeError
    assert smoke.os.environ['HF_HUB_OFFLINE'] == 'previous'
    assert 'TRANSFORMERS_OFFLINE' not in smoke.os.environ


def test_real_web_and_debug_resources(tmp_path):
    smoke._check_web_debug(tmp_path)
