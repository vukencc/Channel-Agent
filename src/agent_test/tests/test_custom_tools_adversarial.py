"""动态工具恶意定义与真实 Linux sandbox 回归；不调用真实模型。"""
import json
import os
import shlex
import socket

import pytest
from pydantic import ValidationError

from agent_core import config
from agent_core.core.custom_tools import CustomToolSpec, custom_registry, validate_custom_tools
from agent_core.tools import TOOL_REGISTRY, command, sandbox


def definition(**changes):
    return {"name": "adversarial_probe", "description": "安全边界探针",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "code": "def main(arguments):\n    return {'ok': True}\n", **changes}


@pytest.mark.parametrize("name", sorted(TOOL_REGISTRY))
def test_every_registered_name_remains_reserved(name):
    with pytest.raises(ValueError, match="覆盖"):
        validate_custom_tools([definition(name=name)])


def test_tool_count_and_utf8_code_exact_boundaries(tmp_path):
    marker = tmp_path / "registration-side-effect"
    prefix = f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef main(arguments): return None\n#"
    code = prefix + "a" * (32000 - len(prefix.encode()))
    assert len(code.encode()) == 32000
    assert len(validate_custom_tools([definition(name=f"probe_{i}", code=code) for i in range(16)])) == 16
    assert not marker.exists()
    with pytest.raises(ValueError, match="16"):
        validate_custom_tools([definition(name=f"probe_{i}") for i in range(17)])
    with pytest.raises(ValidationError):
        CustomToolSpec.model_validate(definition(code=code + "中"))


@pytest.mark.parametrize("code", [
    "class main:\n    def __init__(self, arguments): pass\n",
    "async def main(arguments): return None\n",
    "def main(arguments): return None\ndef main(arguments): return None\n",
    "def main(*arguments): return None\n",
    "def main(arguments=None): return None\n",
    "def main(arguments, *, extra): return None\n",
], ids=["class", "async", "duplicate", "varargs", "default", "keyword-only"])
def test_invalid_main_contract_is_rejected(code):
    with pytest.raises(ValidationError):
        CustomToolSpec.model_validate(definition(code=code))


def test_class_cannot_replace_validated_main():
    code = "def main(arguments): return None\nclass main:\n    def __init__(self, arguments): pass\n"
    with pytest.raises(ValidationError):
        CustomToolSpec.model_validate(definition(code=code))


def nested_schema(depth):
    schema = {"type": "string"}
    for _ in range(depth):
        schema = {"type": "object", "properties": {"value": schema}}
    return schema


@pytest.mark.parametrize("limit", ["depth", "properties", "nodes", "bytes"])
def test_schema_exact_boundaries(limit):
    if limit == "depth":
        valid, invalid = nested_schema(12), nested_schema(13)
    elif limit == "properties":
        valid = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(64)}}
        invalid = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(65)}}
    elif limit == "nodes":
        valid = {"type": "object", "properties": {"a": {"type": "object", "properties": {
            f"p{i}": {"type": "string"} for i in range(63)}}, "b": {"type": "object", "properties": {
            f"p{i}": {"type": "string"} for i in range(62)}}}}
        invalid = json.loads(json.dumps(valid))
        invalid["properties"]["b"]["properties"]["extra"] = {"type": "string"}
    else:
        valid = {"type": "object", "description": ""}
        valid["description"] = "x" * (16384 - len(json.dumps(valid).encode()))
        assert len(json.dumps(valid).encode()) == 16384
        invalid = {**valid, "description": valid["description"] + "x"}
    CustomToolSpec.model_validate(definition(parameters=valid))
    with pytest.raises(ValidationError):
        CustomToolSpec.model_validate(definition(parameters=invalid))


def test_recursive_schema_is_rejected_as_data_without_execution(tmp_path):
    schema = {"type": "object", "properties": {}}
    schema["properties"]["self"] = schema
    marker = tmp_path / "recursive-schema-must-not-run"
    code = f"open({str(marker)!r}, 'w').write('bad')\ndef main(arguments): return None\n"
    with pytest.raises(ValidationError, match="有限 JSON"):
        CustomToolSpec.model_validate(definition(parameters=schema, code=code))
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；原生 Windows 桥接另验")
def test_argument_utf8_exact_byte_limit_executes_and_overflow_does_not(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    schema = {"type": "object", "properties": {"text": {"type": "string"}},
              "required": ["text"], "additionalProperties": False}
    code = ("from pathlib import Path\ndef main(arguments):\n"
            "    path = Path('argument-calls')\n"
            "    path.write_text(str(int(path.read_text()) + 1) if path.exists() else '1')\n"
            "    return {'length': len(arguments['text'])}\n")
    tool = custom_registry([definition(parameters=schema, code=code)])["adversarial_probe"]
    padding = 65536 - len(json.dumps({"text": ""}, ensure_ascii=False).encode())
    text = "中" * (padding // 3) + "x" * (padding % 3)
    arguments = json.dumps({"text": text}, ensure_ascii=False)
    assert len(arguments.encode()) == 65536
    result = tool.run(arguments)
    assert "[退出码] 0" in result and f'"length": {len(text)}' in result, result
    with pytest.raises(ValueError, match="64 KiB"):
        tool.run(json.dumps({"text": text + "x"}, ensure_ascii=False))
    assert (sandbox_env / "argument-calls").read_text() == "1"


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；原生 Windows 桥接另验")
@pytest.mark.parametrize("attack", ["absolute", "traversal", "symlink", "hardlink"])
def test_real_sandbox_denies_external_file_reads(sandbox_env, monkeypatch, attack):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    secret = sandbox_env.parent / "external-secret"
    secret.write_text("external-data-must-not-leak", encoding="utf-8")
    if attack == "symlink":
        (sandbox_env / "alias").symlink_to(secret)
    elif attack == "hardlink":
        os.link(secret, sandbox_env / "alias")
    target = str(secret) if attack == "absolute" else "../external-secret" if attack == "traversal" else "alias"
    code = ("from pathlib import Path\ndef main(arguments):\n"
            f"    return Path({target!r}).read_text()\n")
    result = custom_registry([definition(code=code)])["adversarial_probe"].run("{}")
    assert "[退出码] 0" not in result, result
    assert "external-data-must-not-leak" not in result, result
    assert secret.read_text() == "external-data-must-not-leak"


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；原生 Windows 桥接另验")
@pytest.mark.parametrize("requested_network", [False, True])
def test_network_flag_cannot_override_disabled_network(sandbox_env, monkeypatch, requested_network):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    monkeypatch.setattr(config, "COMMAND_NETWORK", "off")
    # 真实监听端口作阳性对照，避免把未监听端口的拒绝误认作断网。
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=.2):
            connection, _ = listener.accept()
            connection.close()
        code = ("import socket\nfrom pathlib import Path\ndef main(arguments):\n"
                f"    socket.create_connection(('127.0.0.1', {port}), timeout=.2)\n"
                "    Path('network-completed').touch()\n    return 'connected'\n")
        result = custom_registry([definition(code=code, network=requested_network)])["adversarial_probe"].run("{}")
    assert "[退出码] 0" not in result, result
    assert not (sandbox_env / "network-completed").exists()
    if requested_network:
        assert "联网命令未启用" in result


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；原生 Windows 桥接另验")
def test_audit_failure_prevents_custom_code_side_effect(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    def failed_append(*args, **kwargs):
        raise OSError("controlled-audit-unavailable")
    monkeypatch.setattr(sandbox, "append_audit", failed_append)
    code = "from pathlib import Path\ndef main(arguments):\n    Path('unlogged-write').touch()\n    return True\n"
    tool = custom_registry([definition(code=code)])["adversarial_probe"]
    try:
        result = tool.run("{}")
    except OSError as exc:
        assert "controlled-audit-unavailable" in str(exc)
    else:
        assert "[退出码] 0" not in result, result
    assert not (sandbox_env / "unlogged-write").exists()


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；原生 Windows 桥接另验")
@pytest.mark.parametrize("failed_event", ["command_started", "executed"])
@pytest.mark.parametrize("entry", ["custom", "run_command"])
def test_command_audit_failure_preserves_execution_truth(sandbox_env, monkeypatch, failed_event, entry):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    original_append = sandbox.append_audit
    recorded = []
    def fail_selected(path, line, **kwargs):
        event = json.loads(line)["event"]
        recorded.append(event)
        if event == failed_event:
            raise OSError("controlled-selected-audit-failure")
        return original_append(path, line, **kwargs)
    monkeypatch.setattr(sandbox, "append_audit", fail_selected)
    code = "from pathlib import Path\ndef main(arguments):\n    Path('audit-side-effect').touch()\n    return True\n"
    if entry == "custom":
        tool = custom_registry([definition(code=code)])["adversarial_probe"]
        invoke = lambda: tool.run("{}")
    else:
        script = "from pathlib import Path; Path('audit-side-effect').touch()"
        invoke = lambda: command.run_command("python3 -c " + shlex.quote(script))
    if failed_event == "command_started":
        try:
            result = invoke()
        except OSError as exc:
            assert "controlled-selected-audit-failure" in str(exc)
        else:
            assert "[退出码] 0" not in result, result
        assert not (sandbox_env / "audit-side-effect").exists()
    else:
        result = invoke()
        assert (sandbox_env / "audit-side-effect").exists()
        assert result.startswith("[失败]"), result
        assert "已执行" in result or "可能有副作用" in result, result
        assert "[退出码] 0" not in result and "尚未执行" not in result, result
    assert failed_event in recorded
    if entry == "custom":
        assert "custom_tool_execution" in recorded


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；原生 Windows 桥接另验")
def test_combined_maximum_code_and_arguments_fit_real_command(sandbox_env, monkeypatch):
    # 两项各自合法仍须共同适配 Linux 单 argv 限额，防止包装增长造成合法输入无法执行。
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    code = 'def main(arguments): return len(arguments["v"])\n#'
    code += "x" * (32000 - len(code.encode("utf-8")))
    arguments = '{"v":"' + "x" * (65536 - 8) + '"}'
    assert len(code.encode("utf-8")) == 32000
    assert len(arguments.encode("utf-8")) == 65536
    parameters = {"type": "object", "properties": {"v": {"type": "string"}},
                  "required": ["v"], "additionalProperties": False}
    result = custom_registry([definition(code=code, parameters=parameters)])["adversarial_probe"].run(arguments)
    assert "[退出码] 0" in result, result
    assert "[stdout]\n65528" in result, result
