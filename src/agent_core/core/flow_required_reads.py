"""从节点岗位指令识别明确的读取步骤；不授予工具执行权限。"""
from __future__ import annotations

import re
import json

from agent_core.core.flow_action_preflight import selection_only
from agent_core.core.execution_guidance import READ_TOOLS
from agent_core.core.tool_observations import extract_observation

_BUILTIN_READS = READ_TOOLS | {"web_search"}
_CONDITION = re.compile(r"(?:如果|若|假如|倘若|当.{0,20}时|仅在.{0,20}时|必要时|需要时|可选|可以|可调用|可使用|建议|例如|示例|不要|别调用|禁止|无需|不必|不需要|二选一|\b(?:if|when|unless|optional|example|suggest|avoid|do not|don't)\b)", re.I)
_CALL = re.compile(r"^(?:(?:\d+[.、)]|[-*])\s*)?(?:(?:必须|请|首先|先|务必|please|must|first)\s*)*(?:调用|使用|执行|call|invoke|use)\s*[`\"'‘“]*([A-Za-z_][A-Za-z0-9_-]{0,63})[`\"'’”]*(?=$|[\s，。,.;:：、（(])", re.I)
_READ = re.compile(r"(?:读取|读出|查询|检索|获取|只读|\bread\b|\bfetch\b|\bquery\b|\bretrieve\b)", re.I)
_WRITE = re.compile(r"(?:写入|修改|更新|删除|创建|提交|发送|\bwrite\b|\bupdate\b|\bdelete\b|\bcreate\b|\bsend\b)", re.I)


def successful_custom_result(result: object) -> bool:
    """只接受完整命令回执中的一个 JSON 返回值，并排除明确失败。"""
    if not isinstance(result, str) or not result.startswith('[退出码] 0\n[stdout]\n'):
        return False
    output = result[len('[退出码] 0\n[stdout]\n'):].split('\n[stderr]\n', 1)[0].strip()
    if '[输出已截断]' in output or '已截断）' in output:
        return False
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('重复键')
            value[key] = item
        return value
    try:
        value = json.loads(output, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限数')))
    except (ValueError, TypeError, RecursionError):
        return False
    if isinstance(value, dict):
        if value.get('ok') is False or value.get('success') is False:
            return False
        if isinstance(value.get('status'), str) and value['status'].lower() in {'error', 'failed', 'failure', 'cancelled', 'denied'}:
            return False
        if value.get('error') and set(value) <= {'error', 'status', 'ok', 'success', 'code', 'message'}:
            return False
    return True


def successful_builtin_result(name: str, result: object) -> bool:
    """按各内置工具现有的回执协议核验，不把正文解释为状态。"""
    if not isinstance(result, str):
        return False
    if not result:
        return name in {'read_file', 'web_search'}
    observation = extract_observation(result)
    if name == 'read_file':
        return bool(observation and observation['reader_tool'] == name) or result.startswith('[未找到] ')
    if name == 'list_files':
        return result.startswith('[目录] ')
    if name in {'history_read', 'history_search'}:
        try:
            value = json.loads(result)
        except (ValueError, TypeError, RecursionError):
            return False
        return isinstance(value, (list, dict)) and not (isinstance(value, dict) and value.get('error'))
    if name == 'rag_search':
        if result.startswith('[检索状态] '):
            return True
        try:
            return isinstance(json.loads(result), list)
        except (ValueError, TypeError, RecursionError):
            return False
    if name == 'web_search':
        return result.startswith('- [')
    # 未注册的历史名称没有当前可靠回执协议。
    return False


def _first_statement(text: str) -> str:
    """仅在括号外的句界拆分，括号内的分页说明属于同一调用。"""
    depth = 0
    for index, char in enumerate(text):
        if char in '（(':
            depth += 1
        elif char in '）)':
            depth = max(0, depth - 1)
        elif not depth and (char in '；;。' or
                (char == '.' and index > 0 and text[index - 1].isascii()
                 and (text[index - 1].isalpha() or text[index - 1] == '_')
                 and index + 1 < len(text) and text[index + 1].isspace())):
            return text[:index]
    return text


def required_reads(task: str, definitions: list[dict] | None) -> set[str]:
    """只接受有明确调用和读取意图的无条件步骤。"""
    known = set(_BUILTIN_READS)
    for definition in definitions or []:
        if not isinstance(definition, dict):
            continue
        description = definition.get("description", "")
        parameters = definition.get("parameters", {})
        name = definition.get("name")
        # 宽松的 schema 仍可表达选择器意图；仅检查副本，不改变预审或执行 schema。
        intent_schema = dict(parameters) if isinstance(parameters, dict) else {}
        intent_schema.setdefault('additionalProperties', False)
        empty_selector = (intent_schema.get('type') == 'object' and intent_schema.get('properties') == {}
                          and intent_schema.get('required', []) == []
                          and intent_schema.get('additionalProperties') is False
                          and not set(intent_schema) - {'type', 'properties', 'required', 'additionalProperties', 'description', 'title'})
        if (isinstance(name, str) and isinstance(description, str) and isinstance(parameters, dict)
                and (empty_selector or selection_only(intent_schema))
                and _READ.search(description)):
            # 描述只用于辨识用途，实际执行仍按原动态工具权限和预审处理。
            clean = re.sub(r"(?:不(?:做|进行)?(?:任何)?|不会|无需|禁止|not|never|without)\s*(?:修改|写入|更新|删除|创建|提交|write|modify|update|delete|create)", "", description, flags=re.I)
            if not _WRITE.search(clean):
                known.add(name)
    result: set[str] = set()
    conditional_indent: int | None = None
    conditional_list = False
    fenced = False
    for line in task.splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced:
            continue
        indent = len(line) - len(line.lstrip())
        is_list = bool(re.match(r"(?:\d+[.、)]|[-*])\s*", stripped))
        if conditional_indent is not None and (indent < conditional_indent or
                (indent == conditional_indent and not (conditional_list and is_list))):
            conditional_indent = None
            conditional_list = False
        if conditional_indent is not None:
            continue
        # 分号可分隔独立步骤；只对行首的调用语句核验。
        statement = _first_statement(stripped)
        parentheses = re.findall(r"（([^（）]*)）|\(([^()]*)\)", statement)
        # 只有明确限定选择参数的括号条件不改变调用本身的必需性。
        if any(_CONDITION.search(part) and not (re.search(r"\b(?:limit|offset|page|ids?|path|cursor)\b", part, re.I)
                and not re.search(r"调用|执行|call|invoke|客户|用户|要求", part, re.I))
               for pair in parentheses for part in pair if part):
            continue
        main = re.sub(r"（[^（）]*）|\([^()]*\)", "", statement)
        main = re.sub(r"[,，]\s*(?:必要时|需要时)\s*(?:分页|分批)\s*$", "", main)
        if _CONDITION.search(main):
            if stripped.endswith(('：', ':', '。', '.')):
                conditional_indent = indent
                conditional_list = not is_list and stripped.endswith(('：', ':'))
            continue
        match = _CALL.match(main.strip())
        if match:
            name = match.group(1)
            remainder = main.strip()[match.end():]
            alternative = re.search(r"(?:或|或者|\bor\b).*?(?:调用|使用|执行|call|invoke|use)?\s*[`\"'‘“]*([A-Za-z_][A-Za-z0-9_-]{0,63})", remainder, re.I)
            if name in known and not (alternative and alternative.group(1) in known):
                result.add(name)
    return result
