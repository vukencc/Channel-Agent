"""为已观察来源生成有界、可复算的原文引用；不扩大来源权限。"""
import json
import re

MAX_PASSAGES = 64
MAX_PASSAGE_CHARS = 8192
MAX_QUOTE = 160
_SCOPES = {("read_file", "observed_span"), ("rag_search", "observed_span"),
           ("user_input", "user_report"), ("custom_result", "tool_report")}
_KEY_SEPARATOR = re.compile(r"\s*:")


def _unique_pairs(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _sentences(text: str, offset: int):
    # 不在分号、逗号处断开，避免丢失同一句的条件或例外。
    for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text):
        raw = match.group()
        value = raw.strip()
        if value and len(value) <= MAX_QUOTE:
            start = offset + match.start() + len(raw) - len(raw.lstrip())
            yield start, start + len(value), value


def _spans(text: str):
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(text, object_pairs_hook=_unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            return
        # 仅索引无转义的字符串值，保证切片本身就是解码后的原文。
        for match in re.finditer(r'"(?:[^"\\]|\\.)*"', text):
            token = match.group()
            if _KEY_SEPARATOR.match(text, match.end()) or "\\" in token:
                continue
            yield from _sentences(token[1:-1], match.start() + 1)
    else:
        yield from _sentences(text, 0)


def review_passages(sources: list[dict]) -> list[dict]:
    """返回有限提示，不声称覆盖来源；重复或无效来源返回空列表。"""
    if not isinstance(sources, list) or len(sources) > 128:
        return []
    seen = set()
    source_chars = 0
    for row in sources:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"].strip() or len(row["id"]) > 120 or row["id"] in seen
                or not isinstance(row.get("text"), str) or not row["text"]):
            return []
        seen.add(row["id"])
        source_chars += len(row["text"])
        if source_chars > 1_000_000:
            return []
    result = []
    total = 0
    for row in sources:
        if (row.get("kind"), row.get("scope")) not in _SCOPES:
            continue
        for start, end, value in _spans(row["text"]):
            if len(result) >= MAX_PASSAGES or total + len(value) > MAX_PASSAGE_CHARS:
                return result
            result.append({"id": f"E{len(result) + 1}", "source_id": row["id"],
                           "start": start, "end": end, "text": value})
            total += len(value)
    return result


def resolve_passage_issues(verdict: dict, sources: list[dict], targets: list[dict],
                           passages: list[dict]) -> dict | None:
    """仅转换当前完整索引中的 revise；无效或不适用时返回 None。"""
    if (not isinstance(verdict, dict) or set(verdict) != {"status", "issues"}
            or verdict.get("status") != "revise" or not isinstance(verdict.get("issues"), list)
            or not 1 <= len(verdict["issues"]) <= 5 or not isinstance(passages, list)
            or not passages or passages != review_passages(sources)
            or not isinstance(targets, list) or not targets):
        return None
    target_index = {}
    for target in targets:
        if (not isinstance(target, dict) or not isinstance(target.get("id"), str)
                or not target["id"].strip() or len(target["id"]) > 120
                or target["id"] in target_index or not isinstance(target.get("text"), str)
                or not target["text"].strip() or len(target["text"]) > 4000):
            return None
        target_index[target["id"]] = target["text"]
    evidence = {p["id"]: p for p in passages}
    issues = []
    for issue in verdict["issues"]:
        if not isinstance(issue, dict) or set(issue) != {"target_id", "evidence_id", "reason"}:
            return None
        target_id, evidence_id, reason = (issue[k] for k in ("target_id", "evidence_id", "reason"))
        if (not all(isinstance(v, str) and v.strip() for v in (target_id, evidence_id, reason))
                or len(reason) > 300 or target_id not in target_index or evidence_id not in evidence):
            return None
        passage = evidence[evidence_id]
        issues.append({"target_id": target_id, "draft_quote": target_index[target_id],
                       "source_id": passage["source_id"], "quote": passage["text"], "reason": reason})
    return {"status": "revise", "issues": issues}
