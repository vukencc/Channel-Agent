"""Windows 凭据写入期间的目录身份必须保持锁定。"""
import os
from pathlib import Path

import pytest

from agent_core.core.runtime_models import RuntimeModelRegistry


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='验证 Windows 目录句柄保护')


def test_native_model_save_pins_root_before_temporary_key_write(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    registry = RuntimeModelRegistry(root)
    original_open = os.open
    attempted = []

    def replace_root_before_open(path, *args, **kwargs):
        if Path(path).name.startswith('.runtime-models-'):
            attempted.append(path)
            root.rename(tmp_path / 'old-state')
            root.mkdir()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', replace_root_before_open)
    with pytest.raises((ValueError, PermissionError)):
        registry.add(name='测试连接', model='test-model', base_url='https://example.invalid/v1', api_key='synthetic-key')
    assert attempted
    assert not (tmp_path / 'old-state').exists()
    assert not list(root.glob('.runtime-models-*'))
