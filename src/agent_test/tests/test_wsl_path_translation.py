"""WSL 路径映射合批：减少宿主进程启动，仍逐项校验映射与根身份。"""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.tools import wsl_command as bridge
from agent_core.tools.sandbox import SandboxError


def test_translate_many_uses_one_process_and_positional_paths(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, '_launcher', lambda: ['wsl'])
    monkeypatch.setattr(bridge, '_environment', lambda: {'SystemRoot': 'safe'})
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='/mnt/c/中文 空格\n/mnt/d/$literal;name\n')
    monkeypatch.setattr(bridge.subprocess, 'run', run)
    paths = [r'C:\中文 空格', r'D:\$literal;name']
    assert bridge._translate_many(paths) == ['/mnt/c/中文 空格', '/mnt/d/$literal;name']
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[-len(paths):] == paths
    assert not options.get('shell') and options['timeout'] == 20
    assert options['env'] == {'SystemRoot': 'safe'}
    assert paths[0] not in argv[argv.index('-c') + 1]


@pytest.mark.parametrize('output,code', [('', 0), ('/one\n', 0), ('/one\n/two\n/extra\n', 0),
                                       ('/one\n/bad\rpath\n', 0), ('/one\n/two\n', 1)])
def test_translate_many_rejects_incomplete_or_unsafe_results(monkeypatch, output, code):
    monkeypatch.setattr(bridge, '_launcher', lambda: ['wsl'])
    monkeypatch.setattr(bridge.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=code, stdout=output))
    with pytest.raises(SandboxError):
        bridge._translate_many([r'C:\one', r'D:\two'])


def test_translate_many_rejects_control_characters_before_launch(monkeypatch):
    monkeypatch.setattr(bridge.subprocess, 'run', lambda *a, **kw: pytest.fail('不能启动不安全映射'))
    with pytest.raises(SandboxError):
        bridge._translate_many(['C:\\bad\npath'])


@pytest.mark.parametrize('python', ['auto', 'python3', '/custom/python'])
@pytest.mark.parametrize('changed', ['none', 'round_trip', 'identity'])
def test_dispatch_batches_paths_and_keeps_root_checks(tmp_path, monkeypatch, changed, python):
    root = tmp_path / 'workspace'
    root.mkdir()
    worker = tmp_path / 'worker.py'
    monkeypatch.setattr(bridge, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(bridge, 'sandbox_root', lambda: root)
    identities = iter([(1, 2), (1, 3) if changed == 'identity' else (1, 2)])
    monkeypatch.setattr(bridge, 'directory_identity', lambda path: next(identities))
    pinned = []
    def opened(*args, **kwargs):
        pinned.append((args, kwargs))
        return nullcontext()
    monkeypatch.setattr(bridge, 'opened_path', opened)
    monkeypatch.setattr(bridge, '_worker_script', lambda: worker)
    monkeypatch.setattr(bridge, '_launcher', lambda: ['fake-wsl'])
    monkeypatch.setattr(bridge, 'audit', lambda *a, **kw: None)
    monkeypatch.setattr(bridge.config, 'WINDOWS_WSL_PYTHON', python)
    monkeypatch.setattr(bridge.config, 'COMMAND_RUNTIME_PATHS', {})
    monkeypatch.setattr(bridge.config, 'TOOL_ROOTS', {'support': {'path': str(tmp_path / 'kb'), 'readonly': True}})
    forwards, reverses, sent = [], [], []
    def translate_many(paths, **kwargs):
        forwards.append(list(map(str, paths)))
        return ['/mapped/' + str(i) for i in range(len(paths))]
    def translate(path, **kwargs):
        reverses.append((path, kwargs))
        return str(tmp_path / 'other') if changed == 'round_trip' else str(root)
    monkeypatch.setattr(bridge, '_translate_many', translate_many)
    monkeypatch.setattr(bridge, '_translate', translate)
    monkeypatch.setattr(bridge, '_exchange', lambda *a, **kw: sent.append((a, kw)) or 'done')
    if changed == 'none':
        assert bridge.execute_wsl_tool('inspect_environment', {}) == 'done'
        argv = sent[0][0][0]
        expected = ['python3', '-I', '-B', '-u', '/mapped/1', '--auto-python'] if python == 'auto' else [python, '-B', '-u', '/mapped/1']
        assert argv[-len(expected):] == expected
        request = sent[0][0][1]
        assert request['root'] == '/mapped/0'
        assert request['config']['TOOL_ROOTS']['support']['readonly'] is True
        assert request['config']['TOOL_ROOTS']['support']['path'].startswith('/mapped/')
        assert all(value.startswith('/mapped/') for value in request['paths'].values())
    else:
        with pytest.raises(SandboxError):
            bridge.execute_wsl_tool('inspect_environment', {})
        assert not sent
    assert len(forwards) == len(reverses) == 1
    assert len(forwards[0]) == 6  # 工作区、worker、三个保护路径、一个工具根。
    assert reverses[0] == ('/mapped/0', {'reverse': True})
    assert pinned[0][1] == {'directory': True, 'expected_identity': (1, 2)}
