"""Safe, repeatable synchronization of local .env with the template."""

import json
import os
from pathlib import Path
import subprocess
import sys
from io import StringIO

import pytest
from dotenv.main import resolve_variables
from dotenv.parser import parse_stream


def _sync(template, target, *, write=False):
    from agent_test.scripts.sync_env import sync_env
    return sync_env(template, target, write=write)


def test_cli_can_check_environment_with_invalid_application_configuration(tmp_path):
    (tmp_path / '.env.example').write_text('TIMEOUT=30\n', encoding='utf-8')
    target = tmp_path / '.env'
    target.write_text('TIMEOUT=invalid-number\n', encoding='utf-8')
    environment = dict(os.environ, AI_AGENT_PROJECT_ROOT=str(tmp_path))
    environment.pop('TIMEOUT', None)
    result = subprocess.run([sys.executable, '-m', 'agent_test.scripts.sync_env', '--check'],
        cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['preserved'] == 1
    assert target.read_text(encoding='utf-8') == 'TIMEOUT=invalid-number\n'


def _paths(tmp_path, template_text, target_text):
    template = tmp_path / '.env.example'
    target = tmp_path / '.env'
    template.write_bytes(template_text.encode('utf-8'))
    target.write_bytes(target_text.encode('utf-8'))
    return template, target


def _resolved(path, *, override):
    bindings = parse_stream(StringIO(path.read_text(encoding='utf-8')))
    return dict(resolve_variables(((item.key, item.value) for item in bindings
                                   if item.key is not None), override=override))


def test_check_reports_missing_and_local_only_without_touching_file(tmp_path):
    template, target = _paths(tmp_path, '# Service\nA=default\nB=second\n',
                              'B=custom\nEXTRA=private\n')
    original = target.read_bytes()
    report = _sync(template, target)
    assert report['changed'] is True
    assert report['added'] == ['A']
    assert report['preserved'] == 1
    assert report['local_only'] == ['EXTRA']
    assert target.read_bytes() == original


def test_write_follows_template_order_but_preserves_exact_local_assignments(tmp_path):
    template, target = _paths(tmp_path,
        '# First group\nA=template\n# Second group\nB=template\nC=default\n',
        'B="local # literal" # trailing note\nexport A=${HOST_TOKEN}\n')
    report = _sync(template, target, write=True)
    body = target.read_text(encoding='utf-8')
    assert report['changed'] is True
    assert report['added'] == ['C']
    assert 'export A=${HOST_TOKEN}' in body
    assert 'B="local # literal" # trailing note' in body
    assert 'C=default' in body
    assert body.index('# First group') < body.index('export A=${HOST_TOKEN}')
    assert body.index('export A=${HOST_TOKEN}') < body.index('# Second group') < body.index('B="local # literal" # trailing note') < body.index('C=default')


def test_multiline_quotes_json_empty_values_and_interpolation_survive_and_second_write_is_noop(tmp_path):
    template, target = _paths(tmp_path,
        'MULTI="template"\nJSON={}\nEMPTY=template\nREF=template\nNEW=default\n',
        'REF=${EXTERNAL_TOKEN:-fallback}\nEMPTY=\nJSON={"enabled":true,"items":[1,2]}\n'
        'MULTI="first line\nsecond line # literal"\n')
    first = _sync(template, target, write=True)
    body = target.read_bytes()
    assert first['added'] == ['NEW']
    for raw in ('REF=${EXTERNAL_TOKEN:-fallback}', 'EMPTY=',
                'JSON={"enabled":true,"items":[1,2]}',
                'MULTI="first line\nsecond line # literal"'):
        assert raw in body.decode('utf-8')
    second = _sync(template, target, write=True)
    assert second['changed'] is False
    assert second['added'] == []
    assert target.read_bytes() == body


def test_interpolation_order_with_local_token_keeps_resolved_values(tmp_path, monkeypatch):
    monkeypatch.delenv('TOKEN', raising=False)
    template, target = _paths(tmp_path,
        'TOKEN=template\nURL=${TOKEN}/path\nNEW=default\n',
        'TOKEN=local\nURL=${TOKEN}/path\n')
    before = {mode: _resolved(target, override=mode) for mode in (True, False)}
    _sync(template, target, write=True)
    after = {mode: _resolved(target, override=mode) for mode in (True, False)}
    assert before[True]['URL'] == after[True]['URL'] == 'local/path'
    assert before[False]['URL'] == after[False]['URL'] == 'local/path'
    assert 'URL=${TOKEN}/path' in target.read_text(encoding='utf-8')


def test_reordering_that_changes_local_variable_expansion_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv('TOKEN', raising=False)
    template, target = _paths(tmp_path,
        'URL=${TOKEN}/path\nTOKEN=template\n',
        'TOKEN=local\nURL=${TOKEN}/path\n')
    before = target.read_bytes()
    with pytest.raises(ValueError):
        _sync(template, target, write=True)
    assert target.read_bytes() == before


def test_matching_process_environment_cannot_mask_reorder_expansion_change(tmp_path, monkeypatch):
    monkeypatch.setenv('TOKEN', 'local')
    template, target = _paths(tmp_path,
        'TOKEN=template\nURL=${TOKEN}/path\n',
        'URL=${TOKEN}/path\nTOKEN=local\n')
    before = target.read_bytes()
    # With the current process TOKEN=local, both layouts resolve identically;
    # without it, the old URL uses an empty TOKEN and the new URL uses local.
    assert _resolved(target, override=True)['URL'] == 'local/path'
    with pytest.raises(ValueError):
        _sync(template, target, write=True)
    assert target.read_bytes() == before


def test_unset_environment_cannot_mask_future_nonempty_expansion_change(tmp_path, monkeypatch):
    monkeypatch.delenv('TOKEN', raising=False)
    template, target = _paths(tmp_path,
        'TOKEN=\nURL=${TOKEN}/path\n',
        'URL=${TOKEN}/path\nTOKEN=\n')
    before = target.read_bytes()
    assert _resolved(target, override=True)['URL'] == '/path'
    with pytest.raises(ValueError):
        _sync(template, target, write=True)
    assert target.read_bytes() == before


def test_new_template_default_cannot_change_existing_interpolation(tmp_path, monkeypatch):
    monkeypatch.delenv('NEW', raising=False)
    template, target = _paths(tmp_path,
        'NEW=template-default\nKEY=${NEW:-fallback}\n',
        'KEY=${NEW:-fallback}\n')
    before = target.read_bytes()
    assert _resolved(target, override=True)['KEY'] == 'fallback'
    with pytest.raises(ValueError):
        _sync(template, target, write=True)
    assert target.read_bytes() == before


def test_local_only_key_and_its_comment_remain_after_sync(tmp_path):
    template, target = _paths(tmp_path, 'A=template\nB=template\n',
                              'A=custom\n# private deployment note\nPRIVATE_FLAG=yes # keep\n')
    report = _sync(template, target, write=True)
    body = target.read_text(encoding='utf-8')
    assert report['local_only'] == ['PRIVATE_FLAG']
    assert '# private deployment note\nPRIVATE_FLAG=yes # keep' in body
    assert 'A=custom' in body and 'B=template' in body


@pytest.mark.parametrize('bad_side', ['template', 'target'])
def test_duplicate_key_is_rejected_without_modifying_target(tmp_path, bad_side):
    template, target = _paths(tmp_path, 'A=one\nB=two\n', 'A=local\n')
    path = template if bad_side == 'template' else target
    path.write_text('A=one\n# separated duplicate\nexport A=two\n', encoding='utf-8')
    before = target.read_bytes()
    with pytest.raises(ValueError):
        _sync(template, target, write=True)
    assert target.read_bytes() == before


@pytest.mark.parametrize('bad_side', ['template', 'target'])
def test_unclosed_multiline_quote_is_rejected_without_modifying_target(tmp_path, bad_side):
    template, target = _paths(tmp_path, 'A=one\nB=two\n', 'A=local\n')
    path = template if bad_side == 'template' else target
    path.write_text('A="TOP_SECRET_PARSE_VALUE\ncontinued\n', encoding='utf-8')
    before = target.read_bytes()
    with pytest.raises(ValueError) as error:
        _sync(template, target, write=True)
    assert target.read_bytes() == before
    assert 'TOP_SECRET_PARSE_VALUE' not in str(error.value)
    assert any(char.isdigit() for char in str(error.value))


@pytest.mark.parametrize('linked_side', ['template', 'target'])
def test_symbolic_link_input_is_rejected_without_touching_referent(tmp_path, linked_side):
    template, target = _paths(tmp_path, 'A=one\n', 'A=local\n')
    path = template if linked_side == 'template' else target
    referent = tmp_path / f'{linked_side}.real'
    referent.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(referent)
    before = referent.read_bytes()
    with pytest.raises((ValueError, PermissionError, OSError)):
        _sync(template, target, write=True)
    assert referent.read_bytes() == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction')
@pytest.mark.parametrize('with_parent_reference', [False, True])
def test_windows_reparse_parent_is_rejected_without_touching_referent(tmp_path, with_parent_reference):
    referent = tmp_path / 'referent'
    referent.mkdir()
    fixture_dir = tmp_path if with_parent_reference else referent
    template, target = _paths(fixture_dir, 'A=one\n', 'A=local\n')
    junction = tmp_path / 'junction'
    result = subprocess.run(
        ['cmd.exe', '/d', '/c', 'mklink', '/j', str(junction), str(referent)],
        capture_output=True, timeout=5,
    )
    assert result.returncode == 0
    try:
        before = target.read_bytes()
        with pytest.raises(PermissionError):
            route = junction / '..' if with_parent_reference else junction
            _sync(route / template.name, target, write=True)
        assert target.read_bytes() == before
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != 'nt', reason='Windows path handles')
def test_windows_ordinary_parent_reference_can_read_and_write(tmp_path):
    template, target = _paths(tmp_path, 'A=default\nB=default\n', 'A=local\n')
    nested = tmp_path / 'nested'
    nested.mkdir()
    template_path = nested / '..' / template.name
    target_path = nested / '..' / target.name
    assert _sync(template_path, target_path, write=True)['changed'] is True
    assert target.read_bytes() == b'A=local\nB=default\n'


def _assert_private(target):
    if os.name == 'nt':
        from agent_core.core.windows_security import verify_private_path
        verify_private_path(target)
    else:
        assert target.stat().st_mode & 0o777 == 0o600


def test_crlf_content_is_preserved_and_second_write_is_noop(tmp_path):
    template, target = _paths(tmp_path, 'A=default\r\nB=default\r\n',
                              'A=local\r\n')
    _sync(template, target, write=True)
    assert target.read_bytes() == b'A=local\r\nB=default\r\n'
    assert _sync(template, target, write=True)['changed'] is False


def test_mixed_template_and_target_newlines_are_idempotent(tmp_path):
    template, target = _paths(tmp_path, '# group\r\nA=default\r\n\r\nB=default\r\n',
                              'A=local\n')
    _sync(template, target, write=True)
    first = target.read_bytes()
    assert _sync(template, target, write=True)['changed'] is False
    assert target.read_bytes() == first


def test_matching_trailing_blank_records_are_preserved(tmp_path):
    template, target = _paths(tmp_path, 'A=local\n\n', 'A=local\n\n')
    assert _sync(template, target, write=True)['changed'] is False
    assert target.read_bytes() == b'A=local\n\n'


def test_record_newlines_include_indented_blank_lines(tmp_path):
    template, target = _paths(tmp_path,
        'A=default\r\n   \r\nB="first\r\nsecond"\r\n', 'A=local\n')
    _sync(template, target, write=True)
    assert target.read_bytes() == b'A=local\n   \nB="first\r\nsecond"\n'
    assert _sync(template, target, write=True)['changed'] is False


def test_multiline_value_crlf_does_not_change_record_newline(tmp_path):
    template, target = _paths(tmp_path,
        'A=default\r\nMULTI="first\r\nsecond"\r\n', 'A=local\n')
    _sync(template, target, write=True)
    first = target.read_bytes()
    assert first == b'A=local\nMULTI="first\r\nsecond"\n'
    assert _sync(template, target, write=True)['changed'] is False
    assert target.read_bytes() == first


def test_private_unchanged_target_is_not_replaced(tmp_path, monkeypatch):
    template, target = _paths(tmp_path, 'A=local\n', 'A=local\n')
    _sync(template, target, write=True)
    def fail_replace(*args):
        raise AssertionError('unchanged private target was replaced')
    monkeypatch.setattr(os, 'replace', fail_replace)
    assert _sync(template, target, write=True)['changed'] is False


def test_write_creates_missing_target_atomically_with_private_permissions(tmp_path):
    template = tmp_path / '.env.example'
    target = tmp_path / '.env'
    template.write_text('A=default\nB=\n', encoding='utf-8')
    report = _sync(template, target, write=True)
    assert report['changed'] is True
    assert report['added'] == ['A', 'B']
    assert target.read_text(encoding='utf-8').count('A=default') == 1
    _assert_private(target)


def test_existing_target_permissions_are_tightened_to_0600(tmp_path):
    template, target = _paths(tmp_path, 'A=default\nB=default\n', 'A=local\n')
    target.chmod(0o644)
    _sync(template, target, write=True)
    _assert_private(target)


def test_write_tightens_existing_permissions_even_when_content_is_unchanged(tmp_path):
    template, target = _paths(tmp_path, 'A=TOP_SECRET_LOCAL_VALUE\n',
                              'A=TOP_SECRET_LOCAL_VALUE\n')
    target.chmod(0o644)
    before = target.read_bytes()
    report = _sync(template, target, write=True)
    assert report == {'changed': False, 'added': [], 'preserved': 1, 'local_only': []}
    assert target.read_bytes() == before
    _assert_private(target)


@pytest.mark.skipif(os.name != 'nt', reason='Windows DACL')
def test_windows_foreign_acl_is_replaced_even_for_unchanged_content(tmp_path):
    from agent_core.core.windows_security import verify_private_path

    template, target = _paths(tmp_path, 'A=local\n', 'A=local\n')
    result = subprocess.run(['icacls', str(target), '/grant', '*S-1-1-0:(R)'],
                            capture_output=True, timeout=5)
    assert result.returncode == 0
    with pytest.raises(PermissionError):
        verify_private_path(target)
    before = target.read_bytes()
    report = _sync(template, target, write=True)
    assert report['changed'] is False
    assert target.read_bytes() == before
    verify_private_path(target)


@pytest.mark.skipif(os.name != 'nt', reason='Windows DACL')
def test_permission_failure_preserves_original_and_removes_temporary(tmp_path, monkeypatch):
    import agent_test.scripts.sync_env as sync_module

    template, target = _paths(tmp_path, 'A=default\nB=default\n', 'A=local\n')
    before = target.read_bytes()
    names = {entry.name for entry in tmp_path.iterdir()}
    def fail_protection(path):
        raise PermissionError('simulated ACL failure')
    monkeypatch.setattr(sync_module, '_windows_private_temp', fail_protection)
    with pytest.raises(PermissionError, match='simulated ACL failure'):
        _sync(template, target, write=True)
    assert target.read_bytes() == before
    assert {entry.name for entry in tmp_path.iterdir()} == names


@pytest.mark.parametrize('bad_side', ['template', 'target'])
def test_directory_is_rejected_as_nonregular_input(tmp_path, bad_side):
    template, target = _paths(tmp_path, 'A=default\n', 'A=local\n')
    path = template if bad_side == 'template' else target
    path.unlink()
    path.mkdir()
    with pytest.raises((ValueError, PermissionError, OSError)):
        _sync(template, target, write=True)
    assert path.is_dir()


def test_replace_failure_preserves_original_bytes_and_leaves_no_temp_file(tmp_path, monkeypatch):
    template, target = _paths(tmp_path, 'A=default\nB=default\n', 'A=secret\n')
    before = target.read_bytes()
    names_before = {entry.name for entry in tmp_path.iterdir()}
    def fail_replace(*args, **kwargs):
        raise OSError('simulated atomic replacement failure')
    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError, match='replacement failure'):
        _sync(template, target, write=True)
    assert target.read_bytes() == before
    assert {entry.name for entry in tmp_path.iterdir()} == names_before


def test_cli_check_write_exit_codes_and_output_never_reveal_values(tmp_path):
    template, target = _paths(tmp_path, 'A=default\nB=default\n',
                              'A=TOP_SECRET_LOCAL_VALUE\n')
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'sync_env.py'
    base = [sys.executable, str(script), '--template', str(template), '--target', str(target)]
    check = subprocess.run(base, capture_output=True, text=True, timeout=5)
    assert check.returncode == 1
    assert json.loads(check.stdout) == {
        'changed': True, 'added': ['B'], 'preserved': 1, 'local_only': [],
    }
    assert target.read_text(encoding='utf-8') == 'A=TOP_SECRET_LOCAL_VALUE\n'
    write = subprocess.run([*base, '--write'], capture_output=True, text=True, timeout=5)
    assert write.returncode == 0
    assert json.loads(write.stdout) == {
        'changed': True, 'added': ['B'], 'preserved': 1, 'local_only': [],
    }
    same = subprocess.run([*base, '--check'], capture_output=True, text=True, timeout=5)
    assert same.returncode == 0
    assert json.loads(same.stdout) == {
        'changed': False, 'added': [], 'preserved': 2, 'local_only': [],
    }
    for outcome in (check, write, same):
        combined = outcome.stdout + outcome.stderr
        assert 'TOP_SECRET_LOCAL_VALUE' not in combined
        assert 'default' not in combined
    template.write_text('A="unterminated\n', encoding='utf-8')
    invalid = subprocess.run(base, capture_output=True, text=True, timeout=5)
    assert invalid.returncode == 2
