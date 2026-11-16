"""模型 SDK 延迟导入及工具 schema 的离线契约。"""
import os
import subprocess
import sys
from enum import Enum

from pydantic import BaseModel, Field


def test_runtime_import_and_schemas_do_not_load_sdk(tmp_path):
    script = '''
import asyncio, builtins, sys
from pathlib import Path
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name == "openai" or name.startswith("openai."):
        raise AssertionError("startup imported SDK: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = checked
from agent_frontend.desktop.runtime import RuntimeBridge
from agent_core.tools import all_schemas
from agent_core.core.llm import ModelResponseError, ModelConnectionError, close_clients
from agent_core.core.flow_designer import _provider_error_message
from agent_core.core.model_diagnostics import _failure
from agent_core.core.custom_tools import CustomTool
assert RuntimeBridge and all_schemas()
bridge = RuntimeBridge(Path('state'), workspace_root=Path('workspace'))
bridge.shutdown()
asyncio.run(close_clients())
custom = CustomTool({'name': 'custom', 'description': 'd', 'parameters': {'type': 'object', 'properties': {}}})
assert custom.schema['function']['name'] == 'custom'
assert _failure(ModelResponseError('x'), None)[0] == 'response'
assert _provider_error_message(ValueError('x'))
assert not any(name == 'openai' or name.startswith('openai.') for name in sys.modules)
'''
    env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1', 'OPENCODE_API_KEY': 'fixture-key',
           'QT_QPA_PLATFORM': 'offscreen', **{name: 'true' for name in (
               'ENABLE_STRUCTURED_CONTEXT', 'ENABLE_TASK_PLANS', 'ENABLE_AGENT_TASKS',
               'ENABLE_COMMAND_JOBS', 'ENABLE_FILE_EXTRAS')}}
    result = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_all_enabled_catalog_schemas_match_sdk_in_fresh_process(tmp_path):
    script = '''
from agent_core.tools import TOOL_REGISTRY
from openai import pydantic_function_tool
assert len(TOOL_REGISTRY) > 20
for tool in TOOL_REGISTRY.values():
    assert tool.schema == pydantic_function_tool(tool.args_model, name=tool.name), tool.name
'''
    env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1', **{name: 'true' for name in (
        'ENABLE_STRUCTURED_CONTEXT', 'ENABLE_TASK_PLANS', 'ENABLE_AGENT_TASKS',
        'ENABLE_COMMAND_JOBS', 'ENABLE_FILE_EXTRAS')}}
    result = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_concurrent_first_transport_initialization_is_shared(tmp_path):
    script = '''
from concurrent.futures import ThreadPoolExecutor
import threading, time
from agent_core.core import model_transport as transport
original = transport.sdk_httpx
calls = []
def observed():
    calls.append(1)
    time.sleep(.03)
    return original()
transport.sdk_httpx = observed
barrier = threading.Barrier(8)
def initialize(_):
    barrier.wait()
    transport._ensure_transport()
    return transport._SystemTransport, transport.httpx
with ThreadPoolExecutor(max_workers=8) as executor:
    results = list(executor.map(initialize, range(8)))
assert len(calls) == 1
assert all(item[0] is results[0][0] and item[1] is results[0][1] for item in results)
assert issubclass(results[0][0], results[0][1].AsyncBaseTransport)
'''
    env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1'}
    result = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_failed_first_sdk_load_does_not_poison_client_cache(tmp_path):
    script = '''
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from agent_core import config
from agent_core.core import llm
config.API_KEY = 'fixture-key'
config.BASE_URL = 'https://example.invalid/v1'
config.validate_proxy_environment = lambda: None
original = llm.sdk
calls = []
def failing_once():
    calls.append(1)
    if len(calls) == 1:
        raise ImportError('controlled SDK load failure')
    return original()
llm.sdk = failing_once
try:
    llm.get_client()
except ImportError:
    pass
else:
    raise AssertionError('first load did not fail')
assert llm._client is None
barrier = threading.Barrier(8)
def get(_):
    barrier.wait()
    return llm.get_client()
with ThreadPoolExecutor(max_workers=8) as executor:
    clients = list(executor.map(get, range(8)))
assert len(calls) == 2 and all(client is clients[0] for client in clients)
asyncio.run(llm.close_clients())
assert llm._client is None
'''
    env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1'}
    result = subprocess.run([sys.executable, '-c', script], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_catalog_and_nested_schemas_match_installed_sdk():
    from openai import pydantic_function_tool
    from agent_core.tools import TOOL_REGISTRY
    from agent_core.tools.base import Tool

    class Nested(BaseModel):
        value: str

    class Choice(str, Enum):
        first = 'first'
        second = 'second'

    class Recursive(BaseModel):
        child: 'Recursive | None' = None

    class Example(BaseModel):
        nested: Nested = Field(description='nested object')
        optional: int | None = None
        choice: Choice = Choice.first
        renamed: str = Field(default='value', alias='alternate')
        values: list[Nested] = []
        mapping: dict[str, int] = {}
        pair: tuple[int, str] = (1, 'x')
        recursive: Recursive | None = None

    Example.__doc__ = None
    models = [(tool.name, tool.args_model) for tool in TOOL_REGISTRY.values()] + [('example', Example), ('recursive', Recursive)]
    for name, model in models:
        assert Tool(name, model, lambda: None).schema == pydantic_function_tool(model, name=name)
