"""为签发回执校验识别少量可证明无写入的动态读取。"""
import ast
from functools import lru_cache


# 只接受这些已知读取结构。AST 比较忽略注释和排版，同时检查导入、调用和写入模式。
_READER_SHAPES = tuple(ast.dump(ast.parse(code)) for code in (
    """import json, os

def main(arguments):
    path = os.path.join('.', 'feedback.json')
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise ValueError('feedback.json must be an object with items array')
    return data
""",
    """def main(arguments):
    import json
    from pathlib import Path
    rows = json.loads(Path(arguments['path']).read_text(encoding='utf-8'))
    return rows
""",
    """def main(arguments):
    import json
    from pathlib import Path
    rows = json.loads(Path('feedback.json').read_text(encoding='utf-8'))
    return rows
""",
))


@lru_cache(maxsize=128)
def _match_read_code(code: str) -> bool:
    try:
        return ast.dump(ast.parse(code)) in _READER_SHAPES
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
