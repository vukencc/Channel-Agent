"""无工具的多轮 Flow 设计对话。"""
import asyncio
import json
import re

from agent_core import config
from agent_core.core.openai_runtime import loaded_types
from agent_core.core.budgets import BudgetExceeded, budget_scope
from agent_core.core.context import history_size, request_tokens
from agent_core.core.flow_templates import REASONING_EFFORTS, PortableTemplate, flow_tool_catalog, validate_template
from agent_core.core.llm import ModelResponseError, RuntimeModelRequestError
from agent_core.core.model_settings import model_deadline, model_fallbacks, model_profile as profile_scope, model_thinking, model_total_output_chars
from agent_core.core.model_transport import ModelProxyError
from agent_core.core.session_limits import limit, limits_scope
from agent_core.core.tool_settings import model_tools


_FENCE_LINE = re.compile(r"^[ \t]*```([^\r\n]*)\r?$", re.MULTILINE)
_NOTE_MARKUP = re.compile(r'[{}\[\]"`\\<>]')


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError('设计回复的 JSON 含重复字段，无法确定字段值。请重试。')
    return values


def _invalid_constant(_: str) -> None:
    raise ValueError('设计回复含无效 JSON 数值。请重试。')


class _DesignSyntaxError(ValueError):
    """已交付的单份完整草稿，允许模型针对 JSON 语法作一次修正。"""


def _trailing_notes(text: str, fenced: bool) -> bool:
    if not text.strip():
        return True
    return not fenced and text.lstrip(' \t').startswith(('\r\n', '\n')) and not _NOTE_MARKUP.search(text)


def _has_complete_frame(text: str, start: int, fenced: bool) -> bool:
    """仅在严格解析失败后判定草稿边界，不提取字段或更改模型写出的任何内容。"""
    closers = []
    in_string = escaped = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in '{[':
            closers.append('}' if char == '{' else ']')
        elif char in '}]':
            if not closers or closers.pop() != char:
                return False
            if not closers:
                return _trailing_notes(text[position + 1:], fenced)
    return False


def _design_object(response: dict) -> dict:
    """只解码完整独立的一个对象；正文里的引号、花括号及工具源码由 JSON 解码器处理。"""
    if not isinstance(response, dict):
        raise ValueError('模型没有返回设计正文。请重试。')
    if response.get('tool_calls'):
        raise ValueError('设计模型不能调用工具')
    content = response.get('content')
    if not isinstance(content, str) or not content.strip():
        raise ValueError('模型没有返回设计正文。请重试。')
    text = content.strip().removeprefix('\ufeff').strip()
    fences = list(_FENCE_LINE.finditer(text))
    if fences:
        if (len(fences) != 2 or fences[0][1].strip().lower() not in ('', 'json')
                or fences[1][1].strip() or _NOTE_MARKUP.search(text[:fences[0].start()])
                or _NOTE_MARKUP.search(text[fences[1].end():])):
            raise ValueError('设计回复需要一个完整的 JSON 代码块。请重试并只返回一份设计。')
        text = text[fences[0].end():fences[1].start()].strip()
    start = text.find('{')
    prefix = text[:start]
    if start < 0 or (prefix and (fences or not prefix.rstrip(' \t').endswith('\n') or _NOTE_MARKUP.search(prefix))):
        raise ValueError('模型没有返回独立的设计 JSON 对象。请重试并只返回一份设计。')
    decoder = json.JSONDecoder(object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
    try:
        result, end = decoder.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        location = f'第 {exc.lineno} 行第 {exc.colno} 列'
        if _has_complete_frame(text, start, bool(fences)):
            raise _DesignSyntaxError(f'设计回复的 JSON 在{location}存在语法错误。请重试。') from None
        raise ValueError(f'设计回复未形成单份完整 JSON（{location}）。请重试并返回完整模板。') from None
    except RecursionError:
        raise ValueError('设计回复的 JSON 嵌套过深。请减少嵌套后重试。') from None
    if not _trailing_notes(text[end:], bool(fences)):
        raise ValueError('设计回复含额外内容或多份 JSON，无法确定要导入的模板。请重试并只返回一份设计。')
    return result


def _connection_error_message(phase: str | None = None) -> str:
    """连接阶段来自内部指标，不回显服务端、请求或代理异常正文。"""
    if phase == 'stream':
        return ('模型已开始响应，但生成过程中连接中断，本次未导入模板。'
                '请重试；较大流程可分多轮设计。')
    return ('设计请求在首段响应前连接失败，尚未生成模板。请重试；'
            '若持续失败，请检查模型服务与当前进程的网络或代理连接。')


def _provider_error_message(exc: Exception, phase: str | None = None) -> str:
    """只读取 SDK 状态码，不展示服务端响应或请求细节。"""
    if isinstance(exc, RuntimeModelRequestError):
        exc = exc.__context__
    if isinstance(exc, ModelProxyError):
        return 'Windows 系统代理不可用或配置无效；请检查模型连接的代理来源与 Windows 固定代理设置。'
    if isinstance(exc, loaded_types('APITimeoutError')):
        return '设计模型连接或响应等待超时，请检查网络与模型服务后重试'
    if isinstance(exc, loaded_types('APIConnectionError')):
        return _connection_error_message(phase)
    if isinstance(exc, loaded_types('APIStatusError')):
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
    connection_phase = None
    used_fallback = False

    def observe(kind, payload):
        # 保留安全的内部阶段，丢弃模板片段、思考、错误正文和所有其它元数据。
        nonlocal connection_phase, used_fallback
        if kind == 'metrics' and isinstance(payload, dict):
            used_fallback = bool(payload.get('fallbacks'))
            phase = payload.get('failure_phase')
            if isinstance(phase, str) and phase in ('connect', 'stream'):
                connection_phase = phase

    instructions = (
        '你是 Flow 设计助手。只返回一个标准 JSON 对象，顶层字段为 reply（字符串）和 template（便携模板或 null）。'
        '字符串中的换行和引号须按 JSON 转义。不要输出 Markdown、示例或设计过程。需求足以定义岗位和交接时直接生成模板，项目实施细节可在运行时提供。'
        '需要澄清时形如 {"reply":"请说明目标。","template":null}。流程图是有向无环图，每个节点每轮运行一次。各分支表示独立分工，当前节点按拓扑顺序串行运行；交接说明须与实际调度一致。'
        '将测试反馈、评审返工等循环设计为明确的有限步骤：分别实现的工作可分支，在测试节点汇合，再交给修订和复验；写明通过时结束、仍失败时交接未解决项。'
        '为每个岗位写清产物和验收依据，满足交付条件就结束本节点并交接。已有项目契约可引用，不要求重复抄写或核对同一证据。不要创建回边或宣称框架自动无限重跑。未指定模型、执行环境或权限时沿用项目设置。'
        '模板 format 为 agent-flow，version 为 1；节点禁止 workspace_path。不要包含工作区或凭据。'
        '节点可定义 custom_tools，Python 代码提供 main(arguments)，使用标准库或项目依赖完成真实 I/O；不得嵌入凭据。'
        '没有外部 API 时不要声称已发送回复，可写项目 outbox 并说明。设计与导入不会执行代码。'
        '默认每个节点显式填写 tool_names，按职责从可用工具目录选择完成任务所需工具；read_file 和 list_files 是始终保留的基础工具。'
        '需要知识检索时选择 rag_search；需要委派时选择相应的 session、delegate 和 wait 工具。不要默认继承不需要的开发、命令或长任务工具。'
        '需要使用的动态工具名称也须显式加入 tool_names，定义 custom_tools 不等于选用。'
        '动态工具内部通过受控 run_command 执行，部署须允许 run_command，节点无需因此向模型暴露通用 run_command。'
        '用户明确要求继承全部工具时可用 null，并尊重用户明确选择的工具。设计节点提示时要求：按事项引用对应规则，不跨文档迁移角色、期限或条件；对外动作前逐项核验适用条件和证据。'
        '区分已知事实、假设和待核实，不得扩大影响范围；证据不足时如实标记待核实，不得增造。声称外部动作已完成须有权威回执；本地模拟动作须明确标为模拟。'
        '来源自述的事实须保留并标明来源及核实状态，不得因尚未核实而称未提供。已报告与未提供的分类不得互相矛盾；自述但未核实不是未提供，缺口须指出具体未报告的子字段或来源。'
        '来源字段标签和清单须保留原意，不得替换成不同含义。交接中声称已尝试或已检索须对应同一事项的实际调用及结果，不得借用其他事项或次数。'
        '列入未解决或转交前，须将每个缺口关联到原需求中尚未满足的部分；未被要求的辅助信息未知不自动产生新任务。交接与人工升级只覆盖原需求中确实未解决的部分。'
        '已有证据充分回答的咨询，不应仅因普通审核限定语、尚未代办或用户未要求的额外核验就变成待办；下游须复核交接项仍属于原需求范围。模板 schema：'
        + json.dumps(PortableTemplate.model_json_schema(), ensure_ascii=False)
        + '\n可用工具：' + json.dumps(flow_tool_catalog(), ensure_ascii=False)
    )
    history = [{'role': 'system', 'content': instructions}, *messages]

    async def request(turns: list[dict]) -> dict:
        nonlocal connection_phase
        connection_phase = None
        try:
            async with manager.slots:
                return await manager.model(turns, session_id=None, emit=observe)
        except asyncio.CancelledError:
            raise
        except BudgetExceeded:
            raise ValueError('设计请求受费用预算或请求速率限制，请检查预算、模型单价及速率设置后重试') from None
        except TimeoutError:
            raise ValueError('设计模型连接或响应等待超时，请检查网络与模型服务后重试') from None
        except ConnectionError:
            raise ValueError(_connection_error_message(connection_phase)) from None
        except ModelResponseError:
            raise ValueError('设计模型未返回完整可用内容，请精简模板或调整输出配置后重试') from None
        except Exception as exc:
            raise ValueError(_provider_error_message(exc, connection_phase)) from None

    with (profile_scope(model_profile, endpoint), model_tools([]), model_thinking(reasoning_effort),
          model_deadline(None), model_total_output_chars(config.FLOW_DESIGNER_TOTAL_OUTPUT_CHARS),
          budget_scope(manager.budget_ledger, 'flow-designer'), limits_scope({'model_profile': model_profile}, endpoint=endpoint)):
        response = await request(history)
        try:
            result = _design_object(response)
        except _DesignSyntaxError:
            if not limit('MODEL_RECOVERY_LIMIT') or used_fallback:
                raise
            repair = [*history, {'role': 'assistant', 'content': response['content']},
                      {'role': 'user', 'content': '上一份完整草稿有 JSON 语法错误。只纠正语法并返回完整对象。'
                       '保留原设计的节点、边、指令、工具、权限与代码，不新增任务。'}]
            if history_size(repair) > limit('MODEL_INPUT_CHARS') or request_tokens(repair) > limit('MODEL_INPUT_TOKENS'):
                raise ValueError('草稿格式修正超出模型输入预算。请拆分设计需求后重试。') from None
            # 不把上一份草稿发送给其它服务，不将推理正文加入新一轮输入。
            with model_fallbacks(False):
                corrected = await request(repair)
            try:
                result = _design_object(corrected)
            except _DesignSyntaxError as exc:
                raise ValueError(f'一次格式修正后仍未生成可导入的模板。{exc}') from None

    if set(result) != {'reply', 'template'} or not isinstance(result['reply'], str) or len(result['reply']) > 16000:
        raise ValueError('设计结果格式无效，应包含 reply 和 template 两个字段。请重试。')
    try:
        result['reply'].encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('设计回复含无效 Unicode 字符。请重试。') from None
    candidate = result['template']
    if candidate is not None:
        try:
            template = validate_template(candidate, manager)
        except (ValueError, TypeError) as exc:
            if exc.args == ('流程不能有环',):
                raise ValueError('设计模板包含回边。请将反馈展开为测试、修订和复验步骤后重试。') from None
            raise ValueError('设计模板无效，请检查节点、连接或所选工具后重试。') from None
        if not template['nodes']:
            raise ValueError('设计模板至少需要一个节点，请继续澄清需求')
        _layout_generated_template(template, missing_positions=any(
            'x' not in node or 'y' not in node for node in candidate['nodes']))
        for node in template['nodes']:
            node.pop('workspace_path', None)
        result['template'] = template
    return result
