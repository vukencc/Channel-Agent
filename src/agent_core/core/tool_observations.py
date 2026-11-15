"""内置只读工具为工作窗口提供可验证的正文区间；不改变持久回执。"""
from dataclasses import dataclass
import hashlib
import re


@dataclass(frozen=True, slots=True)
class ToolObservation:
    reader_tool: str
    sha256: str
    body_spans: tuple[tuple[int, int], ...]
    recovery_notice: str


class ObservedText(str):
    """仅可信生产者使用的字符串；动态工具的普通文本不能携带此元数据。"""

    def __new__(cls, text: str, observation: ToolObservation):
        value = super().__new__(cls, text)
        object.__setattr__(value, "_observation", observation)
        return value

    @property
    def observation(self) -> ToolObservation:
        return self._observation

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("工具观察元数据不可修改")


def validate_observation(text: str, metadata: object) -> ToolObservation | None:
    """只接受精确版本、文本摘要与有序区间；不从回执正文猜测元数据。"""
    if not isinstance(text, str) or not isinstance(metadata, dict):
        return None
    if set(metadata) != {"version", "reader_tool", "sha256", "body_spans", "recovery_notice"}:
        return None
    if type(metadata["version"]) is not int or metadata["version"] != 1:
        return None
    reader, digest, notice = (metadata["reader_tool"], metadata["sha256"],
                              metadata["recovery_notice"])
    spans = metadata["body_spans"]
    if (not isinstance(reader, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", reader)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(notice, str) or not notice.strip() or len(notice) > 2048
            or not isinstance(spans, (list, tuple)) or not 1 <= len(spans) <= 256):
        return None
    try:
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            return None
        notice.encode("utf-8")
    except UnicodeEncodeError:
        return None
    checked = []
    previous_end = 0
    for span in spans:
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            return None
        start, end = span
        if (type(start) is not int or type(end) is not int
                or start < previous_end or not 0 <= start < end <= len(text)):
            return None
        checked.append((start, end))
        previous_end = end
    return ToolObservation(reader, digest, tuple(checked), notice)


def observe_text(text: str, *, reader_tool: str, body_spans: tuple[tuple[int, int], ...],
                 recovery_notice: str) -> str:
    """不合格的注释退回原始文本，绝不使检索或原始回执丢失。"""
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return text
    metadata = {"version": 1, "reader_tool": reader_tool, "sha256": digest,
                "body_spans": body_spans, "recovery_notice": recovery_notice}
    observation = validate_observation(text, metadata)
    return ObservedText(text, observation) if observation is not None else text


def extract_observation(value: object) -> dict | None:
    """只提取可信类型；模型正文、相似 JSON 或字符串子类均不能冒充。"""
    if type(value) is not ObservedText:
        return None
    observation = value.observation
    metadata = {"version": 1, "reader_tool": observation.reader_tool,
                "sha256": observation.sha256,
                "body_spans": [list(span) for span in observation.body_spans],
                "recovery_notice": observation.recovery_notice}
    return metadata if validate_observation(str(value), metadata) is not None else None
