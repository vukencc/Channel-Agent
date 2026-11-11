"""自定义工具入口的静态绑定契约。"""
import os
import pytest
from pydantic import ValidationError

from agent_core import config
from agent_core.core.custom_tools import CustomToolSpec, custom_registry


def spec(code):
    return {"name": "probe", "description": "probe", "parameters": {"type": "object"}, "code": code}


@pytest.mark.parametrize("binding", [
    "class main: pass", "async def main(arguments): pass", "main = 1",
    "import os as main", "from os import path as main", "if True:\n    main = 1",
    "for main in []: pass", "try:\n    pass\nexcept Exception as main:\n    pass",
])
def test_module_scope_rebinding_rejected(binding):
    with pytest.raises(ValidationError):
        CustomToolSpec.model_validate(spec("def main(arguments): return None\n" + binding))


def test_helper_local_main_is_allowed():
    CustomToolSpec.model_validate(spec("def helper():\n    main = 1\n    return main\ndef main(arguments): return helper()"))


@pytest.mark.skipif(os.name == 'nt', reason='真实 Linux sandbox；原生 Windows WSL 桥接另验')
def test_runtime_rejects_dynamic_non_function_entry(sandbox_env, monkeypatch):
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    code = ("def main(arguments): return None\n"
            "class Replacement:\n    def __init__(self, arguments): pass\n"
            "globals()['main'] = Replacement\n")
    result = custom_registry([spec(code)])["probe"].run("{}")
    assert "[退出码] 0" not in result
    assert "invalid main entry" in result


def test_comprehension_local_main_is_allowed():
    CustomToolSpec.model_validate(spec("values = [main for main in range(2)]\ndef main(arguments): return values"))
