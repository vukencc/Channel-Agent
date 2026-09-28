import json

import pytest

from ai_agent_startup.tools import sandbox


def extras():
    from ai_agent_startup.tools import file_extras
    return file_extras


def test_organize_files_without_command(sandbox_env):
    fs = extras()
    assert '[完成]' in fs.mkdir('notes')
    (sandbox_env / 'a.txt').write_text('你好')
    assert '[完成]' in fs.copy('a.txt', 'notes/b.txt')
    assert '[完成]' in fs.move('notes/b.txt', 'notes/c.txt')
    assert not (sandbox_env / 'notes/b.txt').exists()
    assert (sandbox_env / 'notes/c.txt').read_text() == '你好'
    assert json.loads(fs.stat('notes/c.txt'))['size'] == 6
    assert 'notes/c.txt' in fs.glob('**/*.txt')


@pytest.mark.parametrize('operation,args', [('mkdir', ('new',)), ('copy', ('a', 'b')), ('move', ('a', 'b'))])
def test_denied_writes_preserve_files(sandbox_env, monkeypatch, operation, args):
    (sandbox_env / 'a').write_text('keep')
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    assert '[已取消]' in getattr(extras(), operation)(*args)
    assert sorted(path.name for path in sandbox_env.iterdir()) == ['a']


def test_boundaries_quota_and_destination_race(sandbox_env, tmp_path, monkeypatch):
    fs = extras()
    (sandbox_env / 'a').write_bytes(b'x' * 20)
    (sandbox_env / 'escape').symlink_to(tmp_path)
    assert '[已拦截]' in fs.copy('a', 'escape/out')
    assert '[已拦截]' in fs.glob('../*')
    assert 'escape' not in fs.glob('**/*')
    monkeypatch.setattr(sandbox.config, 'WORKSPACE_LIMIT_MB', 30 / 1024 ** 2)
    assert '[已拦截]' in fs.copy('a', 'b')
    assert not (sandbox_env / 'b').exists()
    monkeypatch.setattr(sandbox.config, 'WORKSPACE_LIMIT_MB', 512)
    def confirm(*_):
        (sandbox_env / 'b').write_text('concurrent')
        return True
    monkeypatch.setattr(sandbox, 'confirmer', confirm)
    assert '[已拦截]' in fs.move('a', 'b')
    assert (sandbox_env / 'b').read_text() == 'concurrent'
    assert (sandbox_env / 'a').exists()
