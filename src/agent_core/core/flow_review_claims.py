"""争议句定位与双向引文校验；不执行模型调用。"""
import hashlib
import json
import re

from agent_core.core.flow_draft_review import (_ASSERTION, _CUSTOMER, _MANDATE, _RULE,
                                               needs_review, review_messages)

INVALID_REVIEW = "审查结果无效或来源不足：请重新确认具体位置和来源，必要时按原权限补读。"
_CROSS = re.compile(r"上述|下述|前述|如下|同上|其余|其他字段|其它字段|\b(?:above|below|previous|following)\b", re.I)


def _pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def claim_targets(draft: str) -> list[dict]:
    """无法确认完整覆盖时返回空列表，调用方必须回退原审查。"""
    if not isinstance(draft, str) or not draft or len(draft) > 500_000:
        return []
    text = draft.strip()
    fenced = text.startswith("```")
    if fenced:
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, re.I | re.S)
        if not match:
            return []
        text = match.group(1)
    fields = []
    structured = text.startswith(("{", "["))
    try:
        if structured or fenced:
            data = json.loads(text, object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, (dict, list)):
                return []
            stack = [(data, "", 0, False)]
            visited = 0
            while stack:
                value, path, depth, sensitive = stack.pop()
                visited += 1
                if visited > 2048 or depth > 8:
                    return []
                if isinstance(value, dict):
                    if sensitive and not value:
                        return []
                    for key, child in reversed(list(value.items())):
                        if needs_review(key):
                            return []
                        key_sensitive = bool(_CROSS.search(key) or any(re.search(pattern, key, re.I)
                                             for pattern in (_CUSTOMER, _ASSERTION, _RULE, _MANDATE)))
                        stack.append((child, path + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1,
                                      sensitive or key_sensitive))
                elif isinstance(value, list):
                    if sensitive and not value:
                        return []
                    for index in range(len(value) - 1, -1, -1):
                        stack.append((value[index], path + "/" + str(index), depth + 1, sensitive))
                elif isinstance(value, str):
                    fields.append((path, value, sensitive))
                elif sensitive:
                    return []
            # 若筛选只在字段拼接后触发，则无法判定跨字段归属。
            if needs_review(text) and not any(needs_review(value) or sensitive for _, value, sensitive in fields):
                return []
        else:
            fields = [("", draft, False)]
    except (ValueError, TypeError, RecursionError):
        return []
    result = []
    for path, value, sensitive in fields:
        if len(value) > 32_000:
            return []
        # 字段名本身可能提供断言主体；整个字段保留其语义。
        if sensitive and value.strip():
            if len(value) > 4000:
                return []
            result.append(_target(path, value, 0, value))
            if len(result) > 64:
                return []
            continue
        if len(value) > 4000 and (needs_review(value) or _CROSS.search(value)
                                  or any(re.search(pattern, value, re.I)
                                         for pattern in (_CUSTOMER, _ASSERTION, _RULE, _MANDATE))):
            return []
        # 数字小数和英文缩写会使简单句界不可靠。
        if re.search(r"\d\.\d|\b[A-Za-z]\.\s*[A-Za-z]\.", value):
            return []
        for match in re.finditer(r"[^。！？.!?\n]+[。！？.!?]?|[^\S\n]*\n", value):
            raw = match.group()
            sentence = raw.strip()
            if not sentence or not (needs_review(sentence) or _CROSS.search(sentence)
                                    or any(re.search(pattern, sentence, re.I)
                                           for pattern in (_CUSTOMER, _ASSERTION, _RULE, _MANDATE))):
                continue
            if len(sentence) > 4000:
                return []
            start = match.start() + len(raw) - len(raw.lstrip())
            end = start + len(sentence)
            result.append(_target(path, sentence, start, value))
            if len(result) > 64:
                return []
    return _number_targets(result)


def _target(path: str, text: str, start: int, context: str) -> dict:
    token = hashlib.sha256((path + "\0" + str(start) + ":" + str(start + len(text)) + "\0" + text).encode("utf-8", "surrogatepass")).hexdigest()[:24]
    if len(context) > 800 and len(text) <= 800:
        spans = [match.span() for match in re.finditer(r"[^。！？.!?\n]+[。！？.!?]?|[^\S\n]*\n", context)]
        covered = [i for i, (left, right) in enumerate(spans) if left <= start < right]
        if covered:
            index = covered[0]
            left, right = spans[index]
            if index and right - spans[index - 1][0] <= 800:
                left = spans[index - 1][0]
            if index + 1 < len(spans) and spans[index + 1][1] - left <= 800:
                right = spans[index + 1][1]
            context = context[left:right]
    return {"id": token, "field": path or "/", "start": start, "end": start + len(text),
            "text": text, "context": context}


def _number_targets(targets: list[dict]) -> list[dict]:
    return [{**target, "id": f"C{index}"} for index, target in enumerate(targets, 1)]


def shared_targets(unit_draft: str) -> list[dict]:
    """只从共享单元的原字段和明确列出的原句定位，失败则回退。"""
    if not isinstance(unit_draft, str) or len(unit_draft) > 500_000:
        return []
    try:
        unit = json.loads(unit_draft, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(unit, dict) or set(unit) != {"field", "claims", "context", "item_ids"}:
            return []
        field, claims, context, item_ids = (unit[k] for k in ("field", "claims", "context", "item_ids"))
        if (not isinstance(field, str) or not field or len(field) > 1024
                or not isinstance(context, str) or not context or len(context) > 4000
                or not isinstance(claims, list) or not 1 <= len(claims) <= 64
                or not isinstance(item_ids, list) or not all(isinstance(i, str) and i for i in item_ids)):
            return []
        path = "/" + field.replace("~", "~0").replace("/", "~1")
        result = []
        for claim in claims:
            if (not isinstance(claim, str) or not claim.strip() or len(claim) > 4000
                    or context.count(claim) != 1):
                return []
            result.append(_target(path, claim, context.index(claim), context))
        if len({row["id"] for row in result}) != len(result):
            return []
        return _number_targets(result)
    except (ValueError, TypeError, RecursionError):
        return []


def claim_messages(draft: str, sources: list[dict], *, targets: list[dict],
                   node_task: str = "", per_item: bool = False, shared: bool = False) -> list[dict]:
    """复用来源白名单和职责边界，单独裁决已标记的争议句。"""
    messages = review_messages(draft, sources, node_task=node_task, per_item=per_item)
    base = messages[0]["content"].split("只输出一个简短 JSON 对象", 1)[0]
    messages[0]["content"] = base + (
        "本次只裁决 targets 标明的完整争议句或歧义字段，草稿及上下文只用于解释；"
        "目标 field 是 JSON Pointer；start/end 是该解码后字段文本的 Unicode 字符区间（左闭右开）。"
        "普通文本的 field 为 /，区间相对于完整草稿。excerpt 只是目标开头的短摘录，"
        "使用完整草稿还原完整目标原文，不得猜测。"
        "跨字段指代须结合完整草稿核对，无法确定归属时请求重新确认；若发现目标外风险，"
        "请以 need_source 标明精确位置及所需资料，无法定位则要求重新确认，不能虚构目标。"
        "自述和岗位判断只核是否冒充客户或政策，允许忠实转述；同一来源的用户证词不等于岗位对影响的分级；"
        "时间的自然改述本身不等于增加精度。仅真正增加主体、范围、精度或必要步骤才可否决。"
        "逐句核对否定和限定。pass 仅在所有目标均有据或确属自述边界且没有新增断言时使用；来源不足用 need_source。"
        "只输出简短 JSON：通过为 {\"status\":\"pass\"}；修改为 "
        "{\"status\":\"revise\",\"issues\":[{\"target_id\":\"目标ID\",\"draft_quote\":\"目标中的逐字摘录\","
        "\"source_id\":\"来源ID\",\"quote\":\"来源逐字引用\",\"reason\":\"具体语义差异\"}]}；"
        "需要资料为 {\"status\":\"need_source\",\"field\":\"精确位置\",\"needed\":\"所需资料或重新确认事项\"}。"
        "不得编造候选或来源引用。"
    )
    if shared:
        messages[0]["content"] += ("共享目标的 location 指向派生草稿的 /context，区间相对于该字段解码后的原文；"
                                    "field 是原草稿字段。共享内容须核对归属，不得将其他事项表述当成本事项事实。")
    if per_item:
        messages[0]["content"] += ("本次只复核当前事项的完整记录和共享备注里归属于它的来源断言。"
                                    "各事项分别核对，整份交接待框架汇总。不要要求当前片段再次提供整个项目的所有记录。")
    payload = json.loads(messages[1]["content"])
    payload["targets"] = [{**{key: target[key] for key in ("id", "field", "start", "end")},
                           "excerpt": target["text"][:64],
                           **({"location": "/context"} if shared else {})} for target in targets]
    messages[1]["content"] = json.dumps(payload, ensure_ascii=False)
    return messages


def claim_feedback(verdict: dict, sources: list[dict], targets: list[dict]) -> str | None:
    """只接受目标和来源两端均可逐字核验的意见。"""
    if not isinstance(targets, list) or not targets or not isinstance(sources, list) or not sources:
        return INVALID_REVIEW
    indexed, target_index = {}, {}
    for rows, index in ((sources, indexed), (targets, target_index)):
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                    or row["id"] in index or not isinstance(row.get("text"), str) or not row["text"]):
                return INVALID_REVIEW
            index[row["id"]] = row["text"]
    if not isinstance(verdict, dict):
        return INVALID_REVIEW
    if verdict == {"status": "pass"}:
        return None
    if set(verdict) == {"status", "field", "needed"} and verdict["status"] == "need_source":
        field, needed = verdict["field"], verdict["needed"]
        if (isinstance(field, str) and field.strip() and len(field) <= 120
                and isinstance(needed, str) and needed.strip() and len(needed) <= 300):
            return f"字段 {field} 需要来源或重新确认：{needed}。请按原权限补读或重新确认。"
        return INVALID_REVIEW
    if set(verdict) != {"status", "issues"} or verdict.get("status") != "revise":
        return INVALID_REVIEW
    issues = verdict["issues"]
    if not isinstance(issues, list) or not 1 <= len(issues) <= 5:
        return INVALID_REVIEW
    feedback = []
    for issue in issues:
        if not isinstance(issue, dict) or set(issue) != {"target_id", "draft_quote", "source_id", "quote", "reason"}:
            return INVALID_REVIEW
        target_id, draft_quote, source_id, quote, reason = (issue[k] for k in
            ("target_id", "draft_quote", "source_id", "quote", "reason"))
        if (not all(isinstance(v, str) and v.strip() for v in (target_id, draft_quote, source_id, quote, reason))
                or len(target_id) > 120 or len(source_id) > 120 or len(draft_quote) > 4000
                or len(quote) > 160 or len(reason) > 300 or target_id not in target_index
                or source_id not in indexed or draft_quote not in target_index[target_id]
                or quote not in indexed[source_id]):
            return INVALID_REVIEW
        feedback.append(f"目标 {target_id}（草稿：{draft_quote}）：{reason}（来源 {source_id}，原文：{quote}）")
    return "请修改草稿：" + "；".join(feedback)
