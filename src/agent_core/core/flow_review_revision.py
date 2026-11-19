"""保守定位修订后的复核字段；无法证明覆盖时回退完整复核。"""
import re
from difflib import SequenceMatcher

from agent_core.core.flow_review_claims import _number_targets, _target
from agent_core.core.flow_review_records import parse_record_json

_CROSS = re.compile(r"上述|下述|前述|如下|同上|其余|其他(?:字段|事项)|其它(?:字段|事项)|"
                    r"\b(?:above|below|previous|following|other items?)\b", re.I)
_IDENTITY = {"id", "feedback_id", "item_id", "record_id", "ticket_id", "item_ref"}
_LINK = re.compile(r"(?:但是|然而|因此|所以|否则|除外|例外|前提|只有|仅当|如果|若|不得|不能|没有|未曾|"
                   r"unless|however|otherwise|except|\b(?:if|not|never)\b)", re.I)
_PRONOUN = re.compile(r"^(?:这|那|该|其|它|上述|前者|后者|this\b|that\b|these\b|those\b|it\b|they\b)", re.I)


def _sentences(text: str) -> list[tuple[int, int, str]] | None:
    """仅在完整句界清晰且没有跨句条件风险时拆分。"""
    if _LINK.search(text) or re.search(r"[\"'“”‘’]|\d\.\d", text):
        return None
    rows = []
    position = 0
    for match in re.finditer(r"[^。！？!?\n]+[。！？!?]", text):
        if text[position:match.start()].strip():
            return None
        raw = match.group()
        value = raw.strip()
        if _PRONOUN.search(value):
            return None
        start = match.start() + len(raw) - len(raw.lstrip())
        rows.append((start, start + len(value), value))
        position = match.end()
    if text[position:].strip() or not 2 <= len(rows) <= 64:
        return None
    if len({row[2] for row in rows}) != len(rows):
        return None
    return rows


def _focused_field(before: str, after: str, issues: list[tuple[int, int]]) -> list[tuple[int, int, str]] | None:
    old_rows, new_rows = _sentences(before), _sentences(after)
    if old_rows is None or new_rows is None:
        return None
    old_issue_indexes = set()
    for start, end in issues:
        indexes = [i for i, (left, right, _) in enumerate(old_rows) if left <= start and end <= right]
        if len(indexes) != 1:
            return None
        old_issue_indexes.add(indexes[0])
    selected = set()
    matcher = SequenceMatcher(None, [row[2] for row in old_rows], [row[2] for row in new_rows], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete" or (tag == "replace" and i2 - i1 != j2 - j1):
            return None
        if tag == "equal":
            selected.update(j1 + index - i1 for index in old_issue_indexes if i1 <= index < i2)
        else:
            selected.update(range(j1, j2))
    return [row for index, row in enumerate(new_rows) if index in selected]


def revision_targets(previous_draft: str, old_issues: list[dict], current_draft: str) -> list[dict] | None:
    """返回需复核的完整字段；None 表示必须使用完整复核。"""
    if (not isinstance(previous_draft, str) or not isinstance(current_draft, str)
            or not previous_draft or not current_draft
            or len(previous_draft) > 500_000 or len(current_draft) > 500_000
            or not isinstance(old_issues, list) or not 1 <= len(old_issues) <= 64):
        return None
    try:
        old = parse_record_json(previous_draft)
        new = parse_record_json(current_draft)
        if not isinstance(old, (dict, list)) or type(old) is not type(new):
            return None
        old_fields = {}
        new_fields = {}
        changed = set()
        stack = [(old, new, "", 0)]
        visited = 0
        while stack:
            before, after, path, depth = stack.pop()
            visited += 1
            if visited > 2048 or depth > 8 or type(before) is not type(after):
                return None
            if isinstance(before, dict):
                if before.keys() - after.keys():
                    return None
                for key, value in after.items():
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    child_path = path + "/" + escaped
                    if _CROSS.search(key):
                        return None
                    if key not in before:
                        if not isinstance(value, str) or not value.strip() or key in _IDENTITY:
                            return None
                        new_fields[child_path] = value
                        changed.add(child_path)
                    else:
                        if key in _IDENTITY and before[key] != value:
                            return None
                        stack.append((before[key], value, child_path, depth + 1))
            elif isinstance(before, list):
                if len(before) != len(after):
                    return None
                for index, (left, right) in enumerate(zip(before, after)):
                    stack.append((left, right, path + "/" + str(index), depth + 1))
            elif isinstance(before, str):
                old_fields[path] = before
                new_fields[path] = after
                if before != after:
                    if not after.strip():
                        return None
                    changed.add(path)
            elif before != after:
                return None
        if any(_CROSS.search(value) for value in (*old_fields.values(), *new_fields.values())):
            return None
        ids = set()
        spans = set()
        issue_fields = {}
        for issue in old_issues:
            if not isinstance(issue, dict):
                return None
            identifier, field = issue.get("id"), issue.get("field")
            start, end, text = issue.get("start"), issue.get("end"), issue.get("text")
            if (not isinstance(identifier, str) or not identifier or identifier in ids
                    or not isinstance(field, str) or field not in old_fields or field not in new_fields
                    or type(start) is not int or type(end) is not int or not isinstance(text, str)
                    or not text.strip() or start < 0 or end <= start or end > len(old_fields[field])
                    or old_fields[field][start:end] != text or (field, start, end) in spans):
                return None
            ids.add(identifier)
            spans.add((field, start, end))
            issue_fields.setdefault(field, []).append((start, end))
            changed.add(field)
        result = []
        total = 0
        for field in sorted(changed):
            text = new_fields[field]
            total += len(text)
            if not text.strip() or len(text) > 4000 or len(field) > 1024 or total > 32_000:
                return None
            focused = (_focused_field(old_fields[field], text, issue_fields.get(field, []))
                       if field in old_fields else None)
            if focused is None:
                result.append(_target(field, text, 0, text))
            else:
                result.extend(_target(field, sentence, start, text) for start, _, sentence in focused)
        return _number_targets(result) if 1 <= len(result) <= 64 else None
    except (ValueError, TypeError, RecursionError):
        return None
