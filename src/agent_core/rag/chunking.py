"""文档分块：父子分块策略。

- 父块（段落）—— 按空行切分，保留完整上下文；
- 子块（句子）—— 把父块再按句末标点切分，用于精确匹配。
"""
import re
from typing import List

from pydantic import dataclasses

# 句子结束符（中英文），用于把父块切成子块
_SENTENCE_ENDINGS = "。！？!?；;…\n"


@dataclasses.dataclass
class TextSplitter:
    """
    父子分块：
        父块（段落）—— 按空行切分，保留完整上下文；
        子块（句子）—— 把父块再按句末标点切分，用于精确匹配。
    """

    parent_chars: int = 600
    child_chars: int = 240

    def __post_init__(self):
        if self.parent_chars < 1 or self.child_chars < 1:
            raise ValueError("分块长度必须为正数")

    @staticmethod
    def _append(chunks: List[tuple[str, int, int]], text: str, start: int, end: int) -> None:
        """收集 text[start:end] 去掉首尾空白后的内容，并记录它在原文中的真实范围。"""
        raw = text[start:end]
        stripped = raw.strip()
        if not stripped:
            return
        lead = len(raw) - len(raw.lstrip())
        chunks.append((stripped, start + lead, start + lead + len(stripped)))

    def split_parents(self, text: str) -> List[tuple[str, int, int]]:
        """
        按空行切分段落，返回 (段落文本, start, end)。
        """
        chunks: List[tuple[str, int, int]] = []
        start = 0

        for match in re.finditer(r"\n\s*\n", text):
            self._append(chunks, text, start, match.start())
            start = match.end()

        self._append(chunks, text, start, len(text))
        return self._bounded(chunks, self.parent_chars)

    def split_children(self, text: str) -> List[tuple[str, int, int]]:
        """
        按句末标点切分句子，返回 (句子文本, start, end)。
        英文句点只在后面是空白或结尾时才算句末，避免把小数（如 0.36）切开。
        """
        chunks: List[tuple[str, int, int]] = []
        start = 0

        for i, ch in enumerate(text):
            is_end = ch in _SENTENCE_ENDINGS
            if not is_end and ch == ".":
                nxt = text[i + 1 : i + 2]
                is_end = nxt == "" or nxt.isspace()
            if is_end:
                self._append(chunks, text, start, i + 1)
                start = i + 1

        self._append(chunks, text, start, len(text))
        return self._bounded(chunks, self.child_chars)

    def _bounded(self, chunks, limit):
        """Bound long spans without discarding text; preserve original offsets."""
        result = []
        for text, start, _ in chunks:
            for offset in range(0, len(text), limit):
                pieces = []
                self._append(pieces, text, offset, min(offset + limit, len(text)))
                result.extend((piece, start + a, start + b) for piece, a, b in pieces)
        return result

    def pack_children(self, text: str) -> List[tuple[str, int, int]]:
        """Pack adjacent sentences into bounded contextual spans without omission."""
        result = []
        start = end = None
        for _, a, b in self.split_children(text):
            if start is not None and b - start > self.child_chars:
                result.append((text[start:end], start, end))
                start = None
            if start is None:
                start = a
            end = b
        if start is not None:
            result.append((text[start:end], start, end))
        return result
