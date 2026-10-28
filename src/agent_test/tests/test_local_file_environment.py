"""本机文件整理跳过沙箱配额，同时保留写入权限和路径边界。"""
import json
from threading import Event

import pytest

from agent_core import config
from agent_core.tools import file_extras, sandbox


@pytest.fixture
def file_context(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'COMMAND_EXECUTION_ENVIRONMENT', 'sandbox')
    monkeypatch.setattr(config, 'TOOL_PERMISSION_RULES', [])
    status = sandbox_env.stat()
    context = sandbox.ToolContext(sandbox_env, config.AUDIT_LOG, lambda *_: True,
                                 Event(), permission_policy='full_access',
                                 root_identity=(status.st_dev, status.st_ino),
                                 execution_environment='local')
    with sandbox.tool_context(context):
        yield context


def _prepare_files(root, limit_mb):
    (root / 'source').write_text('keep-source', encoding='utf-8')
    # 小目标文件旁模拟工具链体积；不需下载真实工具链或触碰用户工作区。
    if limit_mb:
        with (root / 'toolchain-size').open('wb') as stream:
            stream.truncate(limit_mb * 1024 ** 2 + 1)


def _organize(operation, destination='organized'):
    return (file_extras.mkdir(destination) if operation == 'mkdir'
            else getattr(file_extras, operation)('source', destination))


@pytest.mark.parametrize('limit_mb', [0, 1])
@pytest.mark.parametrize('operation', ['mkdir', 'move', 'copy'])
def test_local_file_organization_ignores_sandbox_workspace_quota(
        file_context, monkeypatch, limit_mb, operation):
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', limit_mb)
    _prepare_files(file_context.root, limit_mb)
    result = _organize(operation)
    assert result.startswith('[完成]'), result
    destination = file_context.root / 'organized'
    if operation == 'mkdir':
        assert destination.is_dir()
    else:
        assert destination.read_text() == 'keep-source'
    assert (file_context.root / 'source').exists() is (operation != 'move')


@pytest.mark.parametrize('limit_mb', [0, 1])
@pytest.mark.parametrize('operation', ['mkdir', 'move', 'copy'])
def test_sandbox_file_organization_retains_workspace_quota(
        file_context, monkeypatch, limit_mb, operation):
    file_context.execution_environment = 'sandbox'
    monkeypatch.setattr(config, 'COMMAND_EXECUTION_ENVIRONMENT', 'local')
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', limit_mb)
    _prepare_files(file_context.root, limit_mb)
    result = _organize(operation)
    assert result.startswith('[已拦截]') and 'WORKSPACE_LIMIT_MB' in result, result
    assert not (file_context.root / 'organized').exists()
    assert (file_context.root / 'source').read_text() == 'keep-source'


@pytest.mark.parametrize('operation', ['mkdir', 'move', 'copy'])
def test_local_file_organization_retains_readonly_policy(file_context, monkeypatch, operation):
    file_context.permission_policy = 'readonly'
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    _prepare_files(file_context.root, 0)
    result = _organize(operation)
    assert result.startswith('[已拦截]') and 'readonly' in result, result
    assert not (file_context.root / 'organized').exists()
    assert (file_context.root / 'source').read_text() == 'keep-source'


@pytest.mark.parametrize('mode', ['local', 'sandbox'])
def test_copy_estimated_size_limit_only_applies_to_sandbox(file_context, monkeypatch, mode):
    file_context.execution_environment = mode
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 1)
    payload = b'x' * (600 * 1024)
    (file_context.root / 'source').write_bytes(payload)
    result = file_extras.copy('source', 'organized')
    if mode == 'local':
        assert result.startswith('[完成]'), result
        assert (file_context.root / 'organized').read_bytes() == payload
    else:
        assert result.startswith('[已拦截]') and '复制后将超过' in result, result
        assert not (file_context.root / 'organized').exists()
    assert (file_context.root / 'source').read_bytes() == payload


@pytest.mark.parametrize('operation', ['mkdir', 'move', 'copy'])
def test_local_file_organization_does_not_scan_workspace_size(file_context, monkeypatch, operation):
    _prepare_files(file_context.root, 0)
    monkeypatch.setattr(file_extras, 'check_workspace_quota',
                        lambda: pytest.fail('local 文件整理不能扫描沙箱配额'))
    assert _organize(operation).startswith('[完成]')


@pytest.mark.parametrize('operation', ['mkdir', 'move', 'copy'])
@pytest.mark.parametrize('decision', ['denied', 'cancelled'])
def test_local_file_organization_retains_confirmation_and_cancellation(
        file_context, monkeypatch, operation, decision):
    file_context.permission_policy = 'trusted'
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    _prepare_files(file_context.root, 0)

    def confirm(*_):
        if decision == 'cancelled':
            file_context.cancelled.set()
        return decision != 'denied'

    file_context.confirm = confirm
    result = _organize(operation)
    assert '取消' in result, result
    assert not (file_context.root / 'organized').exists()
    assert (file_context.root / 'source').read_text() == 'keep-source'


@pytest.mark.parametrize('operation', ['mkdir', 'move', 'copy'])
def test_local_file_organization_rechecks_root_identity_after_confirmation(
        file_context, monkeypatch, operation):
    file_context.permission_policy = 'trusted'
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    _prepare_files(file_context.root, 0)
    root = file_context.root
    original = root.with_name('original-project')

    def replace_root(*_):
        root.rename(original)
        root.mkdir()
        (root / 'source').write_text('replacement')
        return True

    file_context.confirm = replace_root
    result = _organize(operation)
    assert result.startswith('[已拦截]') and '替换' in result, result
    assert not (root / 'organized').exists() and not (original / 'organized').exists()
    assert (root / 'source').read_text() == 'replacement'
    assert (original / 'source').read_text() == 'keep-source'


@pytest.mark.parametrize('operation', ['move', 'copy'])
def test_local_transfer_rechecks_source_identity_after_confirmation(
        file_context, monkeypatch, operation):
    file_context.permission_policy = 'trusted'
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    _prepare_files(file_context.root, 0)

    def change_source(*_):
        (file_context.root / 'source').write_text('changed-during-confirmation')
        return True

    file_context.confirm = change_source
    result = _organize(operation)
    assert result.startswith('[已拦截]') and '变化' in result, result
    assert not (file_context.root / 'organized').exists()
    assert (file_context.root / 'source').read_text() == 'changed-during-confirmation'


@pytest.mark.parametrize('operation', ['move', 'copy'])
def test_local_transfer_atomic_publication_does_not_replace_concurrent_target(
        file_context, monkeypatch, operation):
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    _prepare_files(file_context.root, 0)
    original = file_extras.os.link if operation == 'copy' else file_extras._rename_no_replace

    def create_concurrent_target(source, destination):
        destination.write_text('concurrent-target')
        original(source, destination)

    if operation == 'copy':
        monkeypatch.setattr(file_extras.os, 'link', create_concurrent_target)
    else:
        monkeypatch.setattr(file_extras, '_rename_no_replace', create_concurrent_target)
    result = _organize(operation)
    assert result.startswith('[已拦截]'), result
    assert (file_context.root / 'organized').read_text() == 'concurrent-target'
    assert (file_context.root / 'source').read_text() == 'keep-source'
    assert not list(file_context.root.glob('.copy-*'))


@pytest.mark.parametrize('operation', ['mkdir', 'move', 'copy'])
@pytest.mark.parametrize('destination', ['../outside', 'escape/outside'])
def test_local_file_organization_retains_path_boundaries(
        file_context, monkeypatch, tmp_path, operation, destination):
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    _prepare_files(file_context.root, 0)
    (file_context.root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    result = _organize(operation, destination)
    assert result.startswith('[已拦截]') and '越出' in result, result
    assert not (tmp_path / 'outside').exists()
    assert (file_context.root / 'source').read_text() == 'keep-source'


def test_local_stat_and_glob_keep_read_behavior_without_quota_scan(file_context, monkeypatch):
    monkeypatch.setattr(config, 'WORKSPACE_LIMIT_MB', 0)
    _prepare_files(file_context.root, 0)
    monkeypatch.setattr(file_extras, 'check_workspace_quota',
                        lambda: pytest.fail('读取不应扫描沙箱配额'))
    assert json.loads(file_extras.stat('source'))['size'] == len('keep-source')
    assert json.loads(file_extras.glob('*')) == {'paths': ['source'], 'truncated': False}
