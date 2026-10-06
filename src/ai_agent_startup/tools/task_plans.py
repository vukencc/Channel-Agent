"""LLM 计划工具：调用者由上下文确定，私有元数据不能扩权。"""
import asyncio
import copy
import json

from pydantic import BaseModel, ConfigDict, Field

from ai_agent_startup import config
from ai_agent_startup.core.session_service import current_service
from ai_agent_startup.core.task_plans import TaskInput, PendingEdit, Status
from ai_agent_startup.tools.base import register_tool
from ai_agent_startup.tools.sandbox import cancellation_requested


class PlanArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PlanCreateArgs(PlanArgs):
    """复杂任务开始前记录目标、验收条件及步骤依赖；不执行工作，不启动子 Agent。简单请求可跳过计划。已有计划需完成/取消并归档。"""
    goal: str = Field(min_length=1, max_length=4096)
    tasks: list[TaskInput] = Field(min_length=1, max_length=256)
    expected_revision: int = Field(default=0, ge=0)


class PlanGetArgs(PlanArgs):
    """有界分页读取自己的计划、ready 步骤及近期证据 ID。按 next_cursor 翻页；字段缩略有 truncated_fields 标记，完整内容在 CLI/Web 查看。版本冲突先重读，completed 仅自报完成。"""
    cursor: str | None = None
    limit: int = Field(default=32, ge=1, le=256)


class PlanIdentity(PlanArgs):
    plan_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    expected_revision: int = Field(ge=0)


class PlanUpdateArgs(PlanIdentity):
    """开始/阻塞/恢复/完成自己的步骤；一次仅一个前台步骤。完成附真实证据 ID 与结果，不能凭队列结束宣称验收通过。"""
    task_key: str = Field(min_length=1, max_length=64)
    status: Status
    result: str = Field(default='', max_length=8192)
    block_reason: str = Field(default='', max_length=2048)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)


class PlanReviseArgs(PlanIdentity):
    """新发现需要调整计划时追加步骤或修改尚未开始的 pending 步骤；不删除执行历史，不改已开始步骤。"""
    add_tasks: list[TaskInput] = Field(default_factory=list, max_length=256)
    edit_pending_tasks: list[PendingEdit] = Field(default_factory=list, max_length=256)


class PlanBindArgs(PlanIdentity):
    """把前台步骤关联到自己已创建的真实子任务/后台命令（二选一），进入 waiting。只观察执行，不创建/重放任务。"""
    task_key: str = Field(min_length=1, max_length=64)
    agent_task_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    job_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


class PlanArchiveArgs(PlanIdentity):
    """归档已结束且实际执行已排空的计划，保留历史；不隐式取消任何工作。归档后可创建新计划。"""


def _model_response(method, view, arguments):
    """写响应只返版本摘要；读响应按字符预算切完整页，绝不截断 JSON。"""
    maximum = config.TOOL_MAX_OUTPUT
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    def fits(value):
        return len(encode(value)) <= maximum
    result = {key: view[key] for key in ('plan_id', 'revision', 'status', 'verification', 'counts', 'ready')}
    result['ready'] = result['ready'][:16]
    result['ready_more'] = max(0, len(view['ready']) - len(result['ready']))
    if view.get('archived_plan_id'):
        result['archived_plan_id'] = view['archived_plan_id']
    if method != 'get':
        key = arguments.get('task_key')
        if key:
            step = next(item for item in view['tasks'] if item['key'] == key)
            result['affected_step'] = {'key': key, 'status': step['status']}
    else:
        result.update(goal=view['goal'][:160], tasks=[], total_tasks=view['total_tasks'], next_cursor=None,
                      available_evidence=[], evidence_total=len(view['available_evidence']), details_truncated=False)
    # 列表过长不能挤掉身份和步骤详情；ready 总量单独保留。
    reserve = min(600, maximum // 2) if method == 'get' else 0
    while result['ready'] and len(encode(result)) + reserve > maximum:
        result['ready'].pop()
        result['ready_more'] += 1
    if method == 'get':
        start = int(arguments['cursor'].rsplit(':', 1)[1]) if arguments.get('cursor') else 0
        cursor_plan_id = view.get('archived_plan_id') or view['plan_id']
        def compact_step(step):
            value = copy.deepcopy(step)
            shortened = []
            for key, limit in (('title', 160), ('acceptance', 320), ('result', 400), ('block_reason', 160)):
                if len(value[key]) > limit:
                    value[key] = value[key][:limit]
                    shortened.append(key)
            for key in ('depends_on', 'evidence_refs'):
                if len(value[key]) > 8:
                    value[key] = value[key][:8]
                    shortened.append(key)
            if value.get('execution_ref'):
                value['execution_ref'].pop('result_summary', None)
            value.pop('created_at', None)
            value.pop('updated_at', None)
            value['truncated_fields'] = shortened
            return value
        for step in view['tasks']:
            value = compact_step(step)
            result['tasks'].append(value)
            end = start + len(result['tasks'])
            result['next_cursor'] = f'{cursor_plan_id}:{view["revision"]}:{end}' if end < view['total_tasks'] else None
            if not fits(result):
                if len(result['tasks']) > 1:
                    result['tasks'].pop()
                    break
                # 至少一条步骤可前进；超长字段明确缩略，不省略 key/status。
                while not fits(result):
                    strings = [key for key in ('title', 'acceptance', 'result', 'block_reason') if value.get(key)]
                    lists = [key for key in ('depends_on', 'evidence_refs') if value.get(key)]
                    if strings:
                        key = max(strings, key=lambda item: len(value[item]))
                        value[key] = value[key][:len(value[key]) // 2]
                    elif lists:
                        key = lists[0]
                        value[key].pop()
                    elif result['goal']:
                        result['goal'] = result['goal'][:len(result['goal']) // 2]
                        key = 'goal'
                    elif result['ready']:
                        result['ready'].pop()
                        result['ready_more'] += 1
                        key = 'ready'
                    else:
                        raise ValueError('TOOL_MAX_OUTPUT 太小，不能返回计划身份和一条步骤；请提高配置')
                    if key not in value['truncated_fields']:
                        value['truncated_fields'].append(key)
                break
        end = start + len(result['tasks'])
        result['next_cursor'] = f'{cursor_plan_id}:{view["revision"]}:{end}' if end < view['total_tasks'] else None
        for evidence in reversed(view['available_evidence'][-8:]):
            item = {key: evidence[key] for key in ('id', 'source', 'name', 'tool_call_id')}
            item['summary'] = evidence['summary'][:120]
            result['available_evidence'].append(item)
            if not fits(result):
                result['available_evidence'].pop()
                break
        result['details_truncated'] = (len(result['available_evidence']) < result['evidence_total'] or
                                       any(step['truncated_fields'] for step in result['tasks']))
    if not fits(result):
        raise ValueError('TOOL_MAX_OUTPUT 太小，不能返回计划摘要；请提高配置并 plan_get 检查已保存状态')
    return encode(result)


def _bridge(method, **arguments):
    context = current_service()
    service = context.manager.task_plans
    if context.caller_id is None or context.loop is None or service is None:
        raise PermissionError('计划工具需要启用任务计划和有效调用者 Agent')
    async def run():
        session = context.manager.sessions.get(context.caller_id)
        if session is None or cancellation_requested():
            raise PermissionError('调用者已取消或不存在')
        service._gate(session)
        return await getattr(service, method)(session, **arguments)
    result = asyncio.run_coroutine_threadsafe(run(), context.loop).result()
    return _model_response(method, result, arguments)


def plan_create(**arguments):
    return _bridge('create', **arguments)


def plan_get(**arguments):
    return _bridge('get', **arguments)


def plan_update(**arguments):
    return _bridge('update', **arguments)


def plan_revise(**arguments):
    return _bridge('revise', **arguments)


def plan_bind(**arguments):
    return _bridge('bind', **arguments)


def plan_archive(**arguments):
    return _bridge('archive', **arguments)


def register_task_plans():
    for model, function in ((PlanCreateArgs, plan_create), (PlanGetArgs, plan_get),
                            (PlanUpdateArgs, plan_update), (PlanReviseArgs, plan_revise),
                            (PlanBindArgs, plan_bind), (PlanArchiveArgs, plan_archive)):
        register_tool(model, concurrency='control')(function)
