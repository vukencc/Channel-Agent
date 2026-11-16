import json
import threading
import asyncio
import sys
from pathlib import Path

import pytest

from agent_core.core.custom_tools import custom_registry
from agent_core.tools import sandbox


def test_dynamic_confirmation_shows_code_and_arguments_without_changing_audit(tmp_path, monkeypatch):
    from agent_core.tools import command
    from agent_core.tools import execution_environment
    definition = {'name': 'send_reply', 'description': 'send reply',
                  'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']},
                  'code': 'def main(arguments):\n    return arguments["text"]\n', 'network': False}
    definition['code'] = definition['code'].replace('\\n', '\n')
    prompts, executed = [], []
    context = sandbox.ToolContext(tmp_path, tmp_path / 'audit.jsonl',
                                  lambda prompt, timeout: prompts.append(prompt) or False,
                                  threading.Event(), permission_policy='smart', execution_environment='local')
    monkeypatch.setattr(execution_environment, 'validate_local_execution', lambda: tmp_path)
    monkeypatch.setattr(command, 'check_command', lambda command: [command])
    monkeypatch.setattr(command, '_execute', lambda *args, **kwargs: executed.append(args) or 'ok')
    tool = custom_registry([definition])['send_reply']
    with sandbox.tool_context(context):
        result = tool.run(json.dumps({'text': '你好'}, ensure_ascii=False))
    assert '已取消' in result and not executed
    assert 'send_reply' in prompts[0] and '你好' in prompts[0]
    assert definition['code'] in prompts[0] and 'python3 -c' in prompts[0]
    audit_text = (tmp_path / 'audit.jsonl').read_text()
    assert 'python3 -c' in audit_text
    assert definition['code'] not in audit_text and '你好' not in audit_text


def test_review_requires_exact_command_and_restores_on_exception():
    from agent_core.core.command_review import CommandReview, command_review, review_text
    review = CommandReview('actual', 'tool', False, 'source', '{}')
    with pytest.raises(RuntimeError):
        with command_review(review):
            assert review_text('other') is None
            assert review_text('actual', True) is None
            assert 'source' in review_text('actual')
            raise RuntimeError('failure')
    assert review_text('actual') is None


def test_review_scope_isolated_between_concurrent_tasks():
    from agent_core.core.command_review import CommandReview, command_review, review_text
    async def run():
        gate = asyncio.Event()
        async def first():
            with command_review(CommandReview('command', 'first', False, 'first-code', '{}')):
                gate.set()
                await asyncio.sleep(0)
                return review_text('command')
        async def second():
            await gate.wait()
            assert review_text('command') is None
            with command_review(CommandReview('command', 'second', True, 'second-code', '{}')):
                await asyncio.sleep(0)
                return review_text('command')
        one, two = await asyncio.gather(first(), second())
        assert 'first-code' in one and 'second-code' not in one
        assert 'second-code' in two and 'first-code' not in two
        assert review_text('command') is None
    asyncio.run(run())


def test_cancelled_task_resets_review_scope():
    from agent_core.core.command_review import CommandReview, command_review, review_text
    async def run():
        started = asyncio.Event()
        observed = []
        async def worker():
            try:
                with command_review(CommandReview('cmd', 'tool', False, 'source', '{}')):
                    started.set()
                    await asyncio.Event().wait()
            finally:
                observed.append(review_text('cmd'))
        task = asyncio.create_task(worker())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert observed == [None] and review_text('cmd') is None
    asyncio.run(run())


def test_wsl_review_requires_full_original_prompt_and_parameters():
    from agent_core.core.command_review import CommandReview
    from agent_core.tools.wsl_command import _review_confirmation_prompt
    from agent_core.tools.sandbox import format_confirmation_prompt
    review = CommandReview('actual', 'tool', False, 'private source', '{}')
    request = {'operation': 'run_command', 'arguments': {'command': 'actual', 'reason': 'reason', 'network': False}, 'config': {}}
    original = format_confirmation_prompt('run_command', 'actual', 'reason')
    assert 'private source' in _review_confirmation_prompt(original, request, review)
    for forged in (format_confirmation_prompt('other', 'actual', 'reason'),
                   format_confirmation_prompt('run_command', 'other', 'actual'),
                   format_confirmation_prompt('run_command', 'other actual', 'reason'),
                   format_confirmation_prompt('run_command', 'actual\n联网白名单：[]', 'reason')):
        assert _review_confirmation_prompt(forged, request, review) == forged
    for altered in ({**request['arguments'], 'network': True},
                    {**request['arguments'], 'resource_profile': 'development'},
                    {**request['arguments'], 'runtime_profile': 'node'},
                    {**request['arguments'], 'reason': 'different'}):
        assert _review_confirmation_prompt(original, {**request, 'arguments': altered}, review) == original


def test_controlled_native_wsl_dispatch_uses_bound_context(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from agent_core.core.command_review import CommandReview, command_review
    from agent_core.tools import wsl_command as bridge
    monkeypatch.setattr(bridge, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(bridge, 'sandbox_root', lambda: tmp_path)
    monkeypatch.setattr(bridge, 'directory_identity', lambda root: (1, 2))
    monkeypatch.setattr(bridge, 'opened_path', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(bridge, '_translate', lambda path, **kwargs: str(path))
    monkeypatch.setattr(bridge, '_translate_many', lambda paths, **kwargs: list(map(str, paths)))
    monkeypatch.setattr(bridge, '_worker_script', lambda: tmp_path / 'worker')
    monkeypatch.setattr(bridge, '_launcher', lambda: ['fake-wsl'])
    monkeypatch.setattr(bridge, '_settings', lambda **kwargs: {})
    monkeypatch.setattr(bridge, 'audit', lambda *args, **kwargs: None)
    sent = []
    monkeypatch.setattr(bridge, '_exchange', lambda *args, **kwargs: sent.append((args, kwargs)) or 'done')
    review = CommandReview('actual', 'tool', False, 'source', '{}')
    with command_review(review):
        assert bridge.execute_wsl_tool('run_command', {'command': 'actual', 'network': False}) == 'done'
        assert bridge.execute_wsl_tool('run_command', {'command': 'other', 'network': False}) == 'done'
    assert sent[0][1]['review'] is review and not sent[1][1]
    assert 'review' not in sent[0][0][1]


@pytest.mark.parametrize('allowed', [False, True])
def test_smart_sandbox_decision_and_audit_use_original_command(tmp_path, monkeypatch, allowed):
    from types import SimpleNamespace
    from agent_core.core import permissions
    from agent_core.tools import command
    definition = {'name': 'show_data', 'description': 'data', 'parameters': {'type': 'object'},
                  'code': 'def main(arguments):\n    return arguments\n'.replace('\\n', '\n'), 'network': False}
    prompts, risks, executed = [], [], []
    context = sandbox.ToolContext(tmp_path, tmp_path / 'audit.jsonl',
                                  lambda prompt, timeout: prompts.append(prompt) or allowed,
                                  threading.Event(), permission_policy='smart', execution_environment='sandbox')
    # 这是受控 POSIX 决策测试；Windows WSL 桥接由独立用例验证。
    monkeypatch.setattr(command, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(command, 'check_workspace_quota', lambda: None)
    monkeypatch.setattr(command, 'check_command', lambda value: [value])
    monkeypatch.setattr(command, 'isolated_command', lambda argv, **kwargs: argv)
    monkeypatch.setattr(command, '_execute', lambda value, *args, **kwargs: executed.append(value) or 'ok')
    def risk(action, detail, **kwargs):
        risks.append((detail, kwargs['command']))
        return SimpleNamespace(requires_confirmation=True)
    monkeypatch.setattr(permissions, 'evaluate_operation_risk', risk)
    with sandbox.tool_context(context):
        result = custom_registry([definition])['show_data'].run('{"value":"你好"}')
    assert ('ok' if allowed else '已取消') in result
    assert bool(executed) is allowed
    detail, actual = risks[0]
    assert detail == actual and actual.startswith('python3 -c ')
    assert definition['code'] in prompts[0] and '你好' in prompts[0] and actual in prompts[0]
    audit_entries = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert next(row for row in audit_entries if row['event'] == 'confirm')['detail'] == actual
    if allowed:
        assert executed == [actual]


@pytest.mark.skipif(sys.platform != 'linux', reason='使用 Linux worker 协议；Windows 端到端另验')
def test_real_wsl_worker_review_transfers_to_host_confirmation(tmp_path, sandbox_env, monkeypatch):
    from agent_core import config
    from agent_core.core.command_review import bound_review
    from agent_core.tools import command, wsl_command as bridge
    definition = {'name': 'worker_tool', 'description': 'test', 'parameters': {'type': 'object'},
                  'code': 'def main(arguments):\n    return arguments\n'.replace('\\n', '\n'), 'network': False}
    captured = []
    monkeypatch.setattr(command, 'run_command', lambda **kwargs: captured.append((kwargs['command'], bound_review(kwargs['command']))) or 'prepared')
    custom_registry([definition])['worker_tool'].run('{"text":"你好"}')
    actual, review = captured[0]
    settings = {name: getattr(config, name) for name in bridge.CONFIG_KEYS}
    settings.update(COMMAND_TIMEOUT=2, CONFIRM_TIMEOUT=2, COMMAND_RUNTIME_PATHS={}, COMMAND_NETWORK='off', TOOL_ROOTS={})
    request = {'id': 'worker-review', 'operation': 'run_command', 'arguments': {'command': actual},
               'root': str(sandbox_env), 'config': settings, 'policy': 'smart',
               'paths': {name: str(tmp_path / name.lower()) for name in ('PROJECT_ROOT', 'AGENT_STATE_DIR', 'AUDIT_LOG')},
               'execution_environment': 'sandbox', 'flow_workspace_scope': True}
    argv = ['/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'LANG=C.UTF-8',
            'PYTHON_DOTENV_DISABLED=1', 'AI_AGENT_PROJECT_ROOT=' + str(tmp_path / 'worker-state'),
            sys.executable, '-B', '-u', str(Path(bridge.__file__).with_name('wsl_command_worker.py'))]
    prompts = []
    stat = sandbox_env.stat()
    context = sandbox.ToolContext(sandbox_env, tmp_path / 'audit.jsonl', lambda prompt, timeout: prompts.append(prompt) or False,
                                  threading.Event(), permission_policy='smart', root_identity=(stat.st_dev, stat.st_ino),
                                  execution_environment='sandbox', flow_workspace_scope=True)
    with sandbox.tool_context(context):
        result = bridge._exchange(argv, request, context.cancelled, review=review)
    assert '已取消' in result
    assert definition['code'] in prompts[0] and '你好' in prompts[0] and actual in prompts[0]
    assert '[风险操作] run_command：' in prompts[0]
    audit_text = (tmp_path / 'audit.jsonl').read_text()
    assert '你好' not in audit_text and 'worker_tool' not in audit_text
    from dataclasses import replace
    prompts.clear()
    with sandbox.tool_context(context):
        result = bridge._exchange(argv, request, context.cancelled, review=replace(review, command='different'))
    assert '已取消' in result
    assert definition['code'] not in prompts[0] and '你好' not in prompts[0]
    assert actual in prompts[0]
