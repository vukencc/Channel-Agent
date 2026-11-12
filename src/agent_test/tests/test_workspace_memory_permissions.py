"""共享记忆元数据授权不扩大普通工具、工作区路径或只读权限。"""
from threading import Event

import pytest

from agent_core.core.permissions import evaluate_operation_risk
from agent_core.tools.sandbox import ToolContext, ask_permission, tool_context


@pytest.mark.parametrize('path', ['other.json', '.channelagent/other.json',
                                  '@workspace/other.json', '../outside.json'])
def test_metadata_action_cannot_automatically_authorize_other_paths(sandbox_env, tmp_path, path):
    context = ToolContext(sandbox_env, tmp_path / 'audit.jsonl', lambda *_: False,
                          Event(), permission_policy='smart')
    with tool_context(context):
        assert evaluate_operation_risk('workspace_memory_update', paths=[path]).requires_confirmation
        assert not ask_permission('workspace_memory_update', '受控测试', paths=[path])


def test_metadata_action_does_not_relax_normal_overwrite(sandbox_env, tmp_path):
    directory = sandbox_env / '.channelagent'
    directory.mkdir()
    (directory / 'memory.json').write_text('{}', encoding='utf-8')
    context = ToolContext(sandbox_env, tmp_path / 'audit.jsonl', lambda *_: False,
                          Event(), permission_policy='smart')
    with tool_context(context):
        assert not evaluate_operation_risk('workspace_memory_update',
                                          paths=['.channelagent/memory.json']).requires_confirmation
        assert ask_permission('workspace_memory_update', '受控测试', paths=['.channelagent/memory.json'])
        for action in ('update_file', 'edit_file', 'delete_file', 'move'):
            assert evaluate_operation_risk(action, paths=['.channelagent/memory.json']).requires_confirmation


@pytest.mark.parametrize('policy', ['readonly', 'standard', 'trusted'])
def test_metadata_action_preserves_selected_policy(sandbox_env, tmp_path, policy):
    confirmations = []
    def deny(*args):
        confirmations.append(args)
        return False
    context = ToolContext(sandbox_env, tmp_path / 'audit.jsonl', deny, Event(), permission_policy=policy)
    with tool_context(context):
        assert not ask_permission('workspace_memory_update', '受控测试', paths=['.channelagent/memory.json'])
    assert bool(confirmations) == (policy != 'readonly')
    assert not (sandbox_env / '.channelagent').exists()
