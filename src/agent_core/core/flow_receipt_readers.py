"""为签发回执校验识别少量可证明无写入的动态读取。"""
import ast
from functools import lru_cache
from pathlib import PurePosixPath


# 语法仅有这三种形状。导入对象、调用目标、文件模式和关键字始终逐项比较。
_READER_SHAPES = tuple(ast.parse(code) for code in (
    """import json as slot_json, os as slot_os

def main(slot_arguments):
    slot_path = slot_os.path.join('.', 'slot_filename')
    with open(slot_path, 'r', encoding='utf-8') as slot_stream:
        slot_result = slot_json.load(slot_stream)
    if not isinstance(slot_result, dict) or not isinstance(slot_result.get('slot_data_key'), list):
        raise ValueError('slot_diagnostic')
    return slot_result
""",
    """def main(slot_arguments):
    import json as slot_json
    from pathlib import Path as slot_path_class
    slot_result = slot_json.loads(slot_path_class(slot_arguments['slot_path_key']).read_text(encoding='utf-8'))
    return slot_result
""",
    """def main(slot_arguments):
    import json as slot_json
    from pathlib import Path as slot_path_class
    slot_result = slot_json.loads(slot_path_class('slot_filename').read_text(encoding='utf-8'))
    return slot_result
""",
))
_FIXED_NAMES = {'main', 'open', 'dict', 'list', 'isinstance', 'ValueError'}
_PATH_KEYS = {'path', 'file_path', 'input_path', 'source_path'}


def _matches_shape(candidate: ast.AST, shape: ast.AST) -> bool:
    names: dict[str, str] = {}

    def name_matches(actual: str, expected: str) -> bool:
        # 别名和变量必须一直指向同一绑定，也不能覆盖固定调用或其他绑定。
        if expected in names:
            return names[expected] == actual
        if actual.startswith('__') or actual in _FIXED_NAMES or actual in names.values():
            return False
        names[expected] = actual
        return True

    def compare(actual: object, expected: object) -> bool:
        if type(actual) is not type(expected):
            return False
        if isinstance(expected, ast.alias):
            return actual.name == expected.name and name_matches(actual.asname or actual.name, expected.asname)
        if isinstance(expected, ast.Constant) and isinstance(actual.value, str):
            if expected.value == 'slot_filename':
                path = PurePosixPath(actual.value)
                return bool(path.parts) and not path.is_absolute() and '..' not in path.parts and not any(
                    char in actual.value for char in ('\\', '\0'))
            if expected.value == 'slot_path_key':
                return actual.value in _PATH_KEYS
            if expected.value in {'slot_data_key', 'slot_diagnostic'}:
                return True
        if isinstance(expected, ast.AST):
            return all(compare(getattr(actual, field), value) for field, value in ast.iter_fields(expected))
        if isinstance(expected, list):
            return len(actual) == len(expected) and all(compare(left, right) for left, right in zip(actual, expected))
        if isinstance(expected, str) and expected.startswith('slot_'):
            return name_matches(actual, expected)
        return actual == expected

    return compare(candidate, shape)


@lru_cache(maxsize=128)
def _match_read_code(code: str) -> bool:
    try:
        candidate = ast.parse(code)
        return any(_matches_shape(candidate, shape) for shape in _READER_SHAPES)
    except (ValueError, SyntaxError, RecursionError):
        return False


def proven_receipt_reader(definition: dict) -> bool:
    """工具须使用已知读取代码、无网配置和封闭的读取参数。"""
    from agent_core.core.flow_action_preflight import selection_only

    code, schema = definition.get('code'), definition.get('parameters')
    if definition.get('network') is not False or not isinstance(code, str) or not isinstance(schema, dict):
        return False
    empty = schema == {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    return (empty or selection_only(schema)) and _match_read_code(code)


def receipt_reader_backend(platform: str, environment: str) -> bool:
    """Linux 沙箱与 Windows 的 WSL 沙箱使用受信的 Linux 系统解释器。"""
    return environment == 'sandbox' and platform in {'linux', 'win32'}
