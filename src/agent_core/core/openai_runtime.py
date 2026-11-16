"""仅在实际模型请求时加载 SDK；本地错误分类不触发导入。"""
import importlib
import sys
from types import ModuleType


def sdk() -> ModuleType:
    return importlib.import_module('openai')


def loaded_types(*names: str) -> tuple[type, ...]:
    module = sys.modules.get('openai')
    if module is None:
        return ()
    return tuple(value for name in names if isinstance(value := getattr(module, name, None), type))


def sdk_httpx() -> ModuleType:
    base = importlib.import_module('openai._base_client')
    return getattr(base, 'httpx2', None) or base.httpx
