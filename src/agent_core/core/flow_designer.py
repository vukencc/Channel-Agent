"""无工具的多轮 Flow 设计对话。"""
import asyncio
import json

import openai

from agent_core import config
from agent_core.core.budgets import BudgetExceeded, budget_scope
from agent_core.core.flow_templates import REASONING_EFFORTS, PortableTemplate, flow_tool_catalog, validate_template
from agent_core.core.llm import ModelResponseError, RuntimeModelRequestError
from agent_core.core.model_settings import model_profile as profile_scope, model_thinking
from agent_core.core.session_limits import limits_scope
from agent_core.core.tool_settings import model_tools


def _provider_error_message(exc: Exception) -> str:
    """只读取 SDK 状态码，不展示服务端响应或请求细节。"""
    if isinstance(exc, RuntimeModelRequestError):
        exc = exc.__context__
    if isinstance(exc, openai.APITimeoutError):
        return '设计模型请求超时，请稍后重试或缩小需求'
    if isinstance(exc, openai.APIConnectionError):
        return '设计模型连接失败，请检查网络、代理和模型服务后重试'
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status in (400, 422):
            return '模型服务拒绝请求参数，请检查模型及所选推理强度是否受支持'
        if status in (401, 403):
            return '模型服务拒绝授权，请检查 API 密钥及访问权限'
        if status == 404:
            return '模型服务未找到地址或模型，请检查服务地址与模型名称'
        if status == 429:
            return '模型服务触发请求或额度限制，请检查额度并稍后重试'
        if 500 <= status < 600:
            return '模型服务暂时不可用，请检查服务状态并稍后重试'
    return '设计模型请求失败，请检查模型地址、密钥和所选推理强度是否受服务支持，或稍后重试'


def _layout_generated_template(template: dict, *, missing_positions: bool = False) -> None:
    """仅修补新设计的缺省或全部重合坐标，不重新排列已有明确布局。"""
    nodes = template['nodes']
    if len(nodes) < 2 or (not missing_positions and len({(n['x'], n['y']) for n in nodes}) > 1):
        return
    from agent_core.core.flows import _order

    order, incoming = _order(template)
    depths, rows = {}, {}
    by_id = {node['id']: node for node in nodes}
    for identifier in order:
        depth = max((depths[parent] + 1 for parent in incoming[identifier]), default=0)
        row = rows.get(depth, 0)
        by_id[identifier].update(x=64 + depth * 320, y=64 + row * 180)
        depths[identifier] = depth
        rows[depth] = row + 1


async def design_flow(manager, messages: list[dict], *, model_profile=None, reasoning_effort=None):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
        raise ValueError('设计对话消息数量无效')
    for item in messages:
        if (not isinstance(item, dict) or set(item) != {'role', 'content'} or
                item['role'] not in {'user', 'assistant'} or not isinstance(item['content'], str) or
                not 1 <= len(item['content']) <= 16000):
            raise ValueError('设计对话消息无效')
    if sum(len(item['content']) for item in messages) > 64000:
        raise ValueError('设计对话过长')
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError('推理强度无效')
    if model_profile and not manager.model_registry.contains(model_profile):
        raise ValueError('未知模型 profile')
    endpoint = manager.model_registry.resolve(model_profile)
    history = [{'role': 'system', 'content': '你是 Flow 设计助手。仅返回 JSON 对象 {"reply":字符串,"template":便携模板或null}。信息不足时询问并返回 null。模板 format 为 agent-flow，version 为 1；节点禁止 workspace_path。不要包含工作区或凭据。节点可定义 custom_tools，Python 代码提供 main(arguments)，使用标准库或项目依赖完成真实 I/O；不得嵌入凭据。没有外部 API 时不要声称已发送回复，可写项目 outbox 并说明。设计与导入不会执行代码。模板 schema：' + json.dumps(PortableTemplate.model_json_schema(), ensure_ascii=False) + '\n可用工具：' + json.dumps(flow_tool_catalog(), ensure_ascii=False)}] + messages
    try:
        with profile_scope(model_profile, endpoint), model_tools([]), model_thinking(reasoning_effort), budget_scope(manager.budget_ledger, 'flow-designer'), limits_scope({'model_profile': model_profile}, endpoint=endpoint):
            async with asyncio.timeout(config.MODEL_CALL_TIMEOUT):
                async with manager.slots:
                    response = await manager.model(history, session_id=None, emit=lambda *_: None)
    except asyncio.CancelledError:
        raise
    except BudgetExceeded:
        raise ValueError('设计请求受费用预算或请求速率限制，请检查预算、模型单价及速率设置后重试') from None
    except TimeoutError:
        raise ValueError('设计模型请求超时，请稍后重试或缩小需求') from None
    except ConnectionError:
        raise ValueError('设计模型连接失败，请检查网络、代理和模型服务后重试') from None
    except ModelResponseError:
        raise ValueError('设计模型未返回完整可用内容，请精简模板或调整输出配置后重试') from None
    except Exception as exc:
        raise ValueError(_provider_error_message(exc)) from None
    try:
        if response.get('tool_calls'):
            raise ValueError('设计模型不能调用工具')
        result = json.loads(response['content'])
        if not isinstance(result, dict) or set(result) != {'reply', 'template'} or not isinstance(result['reply'], str) or len(result['reply']) > 16000:
            raise ValueError('设计结果格式无效')
        if result['template'] is not None:
            candidate = result['template']
            try:
                result['template'] = validate_template(candidate, manager)
            except (ValueError, TypeError):
                raise ValueError('设计模板无效，请调整需求后重试') from None
            if not result['template']['nodes']:
                raise ValueError('设计模板至少需要一个节点，请继续澄清需求')
            _layout_generated_template(result['template'], missing_positions=any(
                'x' not in node or 'y' not in node for node in candidate['nodes']))
            for node in result['template']['nodes']:
                node.pop('workspace_path', None)
        return result
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('设计模型返回无效 JSON') from exc
