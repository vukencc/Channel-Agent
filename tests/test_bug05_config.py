import pytest
from ai_agent_startup import config


def test_invalid_number_identifies_variable(monkeypatch):
    monkeypatch.setenv('TEST_INTEGER', 'wrong')
    with pytest.raises(ValueError, match='TEST_INTEGER'):
        config.env_int('TEST_INTEGER', 1)


def test_runtime_validation_rejects_missing_key_and_bad_ranges(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'API_KEY', '')
    monkeypatch.setattr(config, 'COMMAND_TIMEOUT', -1)
    monkeypatch.setattr(config, 'DOC_DIR', tmp_path)
    with pytest.raises(ValueError, match='OPENCODE_API_KEY') as caught:
        config.validate_runtime_config()
    assert 'COMMAND_TIMEOUT' in str(caught.value)


def test_runtime_validation_accepts_valid_without_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, 'API_KEY', 'test')
    monkeypatch.setattr(config, 'BASE_URL', 'https://example.org/v1')
    monkeypatch.setattr(config, 'MODEL', 'model')
    monkeypatch.setattr(config, 'DOC_DIR', tmp_path)
    monkeypatch.setattr(config, 'SANDBOX_DIR', tmp_path / 'new')
    config.validate_runtime_config()
    assert not capsys.readouterr().out
