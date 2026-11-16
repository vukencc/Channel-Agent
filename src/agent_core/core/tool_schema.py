"""不依赖模型 SDK 的 Pydantic 严格工具 schema。"""
from typing import Any

from pydantic import BaseModel


def _strict(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    for section in ('$defs', 'definitions'):
        if isinstance(schema.get(section), dict):
            for child in schema[section].values():
                _strict(child, root)
    if schema.get('type') == 'object' and 'additionalProperties' not in schema:
        schema['additionalProperties'] = False
    if isinstance(schema.get('properties'), dict):
        schema['required'] = list(schema['properties'])
        schema['properties'] = {key: _strict(value, root) for key, value in schema['properties'].items()}
    if isinstance(schema.get('items'), dict):
        schema['items'] = _strict(schema['items'], root)
    if isinstance(schema.get('anyOf'), list):
        schema['anyOf'] = [_strict(value, root) for value in schema['anyOf']]
    if isinstance(schema.get('allOf'), list):
        if len(schema['allOf']) == 1:
            schema.update(_strict(schema['allOf'][0], root))
            schema.pop('allOf')
        else:
            schema['allOf'] = [_strict(value, root) for value in schema['allOf']]
    if 'default' in schema and schema['default'] is None:
        schema.pop('default')
    ref = schema.get('$ref')
    if ref and len(schema) > 1:
        assert isinstance(ref, str), f'Received non-string $ref - {ref}'
        if not ref.startswith('#/'):
            raise ValueError(f'Unexpected $ref format {ref!r}; Does not start with #/')
        resolved = root
        for key in ref[2:].split('/'):
            resolved = resolved[key]
            assert isinstance(resolved, dict)
        schema.update({**resolved, **schema})
        schema.pop('$ref')
        return _strict(schema, root)
    return schema


def function_schema(model: type[BaseModel], name: str) -> dict:
    parameters = model.model_json_schema()
    function = {'name': name, 'strict': True, 'parameters': _strict(parameters, parameters)}
    if model.__doc__ is not None:
        function['description'] = model.__doc__
    return {'type': 'function', 'function': function}
