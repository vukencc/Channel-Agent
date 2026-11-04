"""Flow 工具目录与便携格式校验。"""
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core import config
from agent_core.core.flows import Edge, Definition, _order
from agent_core.core.tool_settings import model_catalog
from agent_core.tools import all_schemas

REQUIRED_TOOL_NAMES = ['read_file', 'list_files']
REASONING_EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh']
THINKING_MODES = ['auto', 'enabled', 'disabled']


def flow_tool_catalog():
    rows = model_catalog(all_schemas(), config.MODEL_TOOL_NAMES)
    return {'tools': [{'name': row['function']['name'],
                       'description': row['function'].get('description', ''),
                       'required': row['function']['name'] in REQUIRED_TOOL_NAMES} for row in rows],
            'required_tool_names': list(REQUIRED_TOOL_NAMES),
            'reasoning_efforts': list(REASONING_EFFORTS), 'thinking_modes': list(THINKING_MODES)}


class PortableNode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=16000)
    x: float = Field(default=0, allow_inf_nan=False)
    y: float = Field(default=0, allow_inf_nan=False)
    handoff: str = Field(default='', max_length=16000)
    handoff_file: str | None = Field(default=None, max_length=4096)
    model_profile: str | None = Field(default=None, max_length=200)
    tool_names: list[str] | None = Field(default=None, max_length=256)
    reasoning_effort: Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh'] | None = None
    thinking_mode: Literal['auto', 'enabled', 'disabled'] | None = None
    reuse_session: bool = Field(default=False, strict=True)


class PortableTemplate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['agent-flow']
    version: Literal[1]
    title: str = Field(min_length=1, max_length=120)
    handoff: str = Field(default='', max_length=16000)
    execution_environment: Literal['sandbox', 'local'] | None = None
    permission_policy: Literal['full_access', 'smart', 'readonly'] | None = None
    model_profile: str | None = Field(default=None, max_length=200)
    nodes: list[PortableNode] = Field(max_length=128)
    edges: list[Edge] = Field(max_length=1024)

    @field_validator('version', mode='before')
    @classmethod
    def validate_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('不支持的 Flow 模板版本')
        return value


def validate_template(payload, manager, *, missing_models='error'):
    if missing_models not in {'error', 'inherit'}:
        raise ValueError('missing_models 必须为 error 或 inherit')
    if isinstance(payload, dict) and isinstance(payload.get('nodes'), list):
        if any(isinstance(node, dict) and 'workspace_path' in node for node in payload['nodes']):
            raise ValueError('便携模板不能包含工作区')
    value = PortableTemplate.model_validate(payload).model_dump()
    if value['format'] != 'agent-flow' or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('不支持的 Flow 模板格式或版本')
    for node in value['nodes']:
        path = node['handoff_file']
        if path and (PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive or PureWindowsPath(path).is_absolute() or '..' in PurePosixPath(path.replace('\\', '/')).parts):
            raise ValueError('交接文件必须为项目内相对路径')
        if node['tool_names'] is not None:
            available = {item['name'] for item in flow_tool_catalog()['tools']}
            if not set(node['tool_names']) | set(REQUIRED_TOOL_NAMES) <= available:
                raise ValueError('节点工具超出部署模型工具目录')
    for item in [value, *value['nodes']]:
        name = item['model_profile']
        if name and not manager.model_registry.contains(name):
            if missing_models == 'error':
                raise ValueError(f'模型 profile 不存在：{name}')
            item['model_profile'] = None
    definition = Definition.model_validate({**{key: val for key, val in value.items() if key not in {'format', 'version'}},
                                            'id': '0' * 32, 'revision': 0}).model_dump()
    _order(definition)
    return value
