"""动态工具的参数、实际 I/O、确认及项目边界回归。"""
import json
import os
import threading

import pytest
from pydantic import ValidationError

from agent_core import config
from agent_core.core.custom_tools import CustomToolSpec, custom_registry, validate_custom_tools
from agent_core.tools import sandbox


def spec(**changes):
    result = {
        "name": "write_reply", "description": "将回复写到项目发件箱",
        "parameters": {"type": "object", "properties": {"reply": {"type": "string"}},
                       "required": ["reply"], "additionalProperties": False},
        "code": ("from pathlib import Path\n"
                 "def main(arguments):\n"
                 "    Path('reply.txt').write_text(arguments['reply'], encoding='utf-8')\n"
                 "    return {'saved': True, 'reply': arguments['reply']}\n"),
        "network": False,
    }
    return {**result, **changes}


@pytest.mark.parametrize("parameters", [
    {"type": "array"},
    {"type": ["object", "null"]},
    {"type": "object", "properties": {"x": {"$ref": "https://example.invalid/schema"}}},
    {"type": "object", "properties": {"x": {"type": "string", "pattern": "(a+)+$"}}},
    {"type": "object", "additionalProperties": {"type": "string"}},
    {"type": "object", "required": "x"},
    {"type": "object", "properties": {"x": {"enum": []}}},
])
def test_unsupported_schema_rejected_without_execution(parameters):
    with pytest.raises(ValidationError):
        CustomToolSpec.model_validate(spec(parameters=parameters))


def test_definition_is_data_and_cannot_override_builtin(tmp_path):
    marker = tmp_path / "must-not-exist"
    definition = spec(code=f"open({str(marker)!r}, 'w').write('side effect')\ndef main(arguments): return None")
    validate_custom_tools([definition])
    assert not marker.exists()
    with pytest.raises(ValueError, match="覆盖"):
        validate_custom_tools([spec(name="read_file")])
    with pytest.raises(ValueError, match="重复"):
        validate_custom_tools([spec(), spec()])


def test_arguments_checked_before_command(monkeypatch):
    called = []
    monkeypatch.setattr("agent_core.tools.command.run_command", lambda **kwargs: called.append(kwargs))
    tool = custom_registry([spec()])["write_reply"]
    for arguments in ('{"reply":12}', '{"reply":"ok","extra":true}', '{"reply":NaN}', "[]"):
        with pytest.raises(ValueError):
            tool.run(arguments)
    numeric = custom_registry([spec(parameters={
        'type': 'object', 'properties': {'value': {'type': 'number'}}, 'required': ['value'],
    })])['write_reply']
    with pytest.raises(ValueError):
        numeric.run('{"value":1e999}')
    assert called == []


def test_schema_edit_is_revalidated_and_multibyte_code_is_bounded():
    definition = spec()
    validate_custom_tools([definition])
    definition['parameters']['required'] = 'invalid-after-first-validation'
    with pytest.raises(ValidationError):
        validate_custom_tools([definition])
    code = 'def main(arguments): return None\n#' + '😀' * 8000
    assert len(code) < 32000
    with pytest.raises(ValidationError, match='UTF-8'):
        CustomToolSpec.model_validate(spec(code=code))


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux 隔离运行，Windows 桥接另验")
def test_real_custom_tool_writes_only_selected_project(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    original = sandbox_env.parent / "private.txt"
    original.write_text("private", encoding="utf-8")
    monkeypatch.setenv("CUSTOM_TOOL_HOST_SECRET", "private")
    tool = custom_registry([spec()])["write_reply"]
    reply = "'; touch /tmp/should-not-exist; echo 你好\n"
    result = tool.run(json.dumps({"reply": reply}))
    assert "[退出码] 0" in result, result
    assert (sandbox_env / "reply.txt").read_text(encoding="utf-8") == reply
    probe = custom_registry([spec(name="check_boundary", code=(
        "import os\nfrom pathlib import Path\n"
        "def main(arguments):\n"
        "    assert 'CUSTOM_TOOL_HOST_SECRET' not in os.environ\n"
        f"    assert not Path({str(original)!r}).exists()\n"
        "    assert not Path('../private.txt').exists()\n"
        "    return {'isolated': True}\n"
    ))])["check_boundary"]
    assert '"isolated": true' in probe.run('{"reply":"check"}')
    assert original.read_text(encoding="utf-8") == "private"


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux 隔离运行，Windows 桥接另验")
def test_custom_tool_preserves_confirmation_denial_and_cancellation(sandbox_env):
    info = sandbox_env.stat()
    cancelled = threading.Event()
    context = sandbox.ToolContext(
        sandbox_env, sandbox_env.parent / "audit.jsonl", lambda *_: False, cancelled,
        permission_policy="smart", root_identity=(info.st_dev, info.st_ino),
        execution_environment="sandbox", flow_workspace_scope=True)
    tool = custom_registry([spec()])["write_reply"]
    with sandbox.tool_context(context):
        result = tool.run('{"reply":"denied"}')
        assert "[已取消]" in result, result
        assert not (sandbox_env / "reply.txt").exists()
        cancelled.set()
        assert "[已取消]" in tool.run('{"reply":"cancelled"}')
    assert not (sandbox_env / "reply.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux 隔离运行，Windows 桥接另验")
@pytest.mark.parametrize("text", ["😀" * 12000, "'" * 60000], ids=['unicode', 'quotes'])
def test_real_custom_tool_accepts_large_arguments_under_byte_limit(sandbox_env, monkeypatch, text):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    code = "def main(arguments):\n    return {'length': len(arguments['reply'])}\n"
    # 合法代码与参数接近各自上限，仍须避开操作系统单个 argv 的长度限制。
    code += "#" + "x" * (32000 - len(code) - 1)
    tool = custom_registry([spec(code=code)])["write_reply"]
    arguments = json.dumps({"reply": text}, ensure_ascii=False)
    assert len(arguments.encode()) < 65536
    result = tool.run(arguments)
    assert "[退出码] 0" in result, result
    assert f'"length": {len(text)}' in result
