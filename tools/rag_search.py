"""RAG 检索工具：从本地知识库检索相关片段。"""
from typing import Literal

from pydantic import BaseModel, Field

from rag.tool import rag_search as _rag_search
from tools.base import register_tool
from core.log import get_logger
from rag.index import get_index
from tools.sandbox import read_only_root, SandboxError, audit

logger = get_logger(__name__)


class RagSearchArgs(BaseModel):
    """
    从本地知识库中检索与问题最相关的文本片段。当需要回答关于知识库内容的问题时调用。

    完整执行向量召回、BM25、RRF 融合和本地模型重排。
    返回命中数量及各阶段分数；重排 logit 不是概率，不同阶段分数不能直接比较。
    请据内容和重排阈值判断是否需要重试：
    1. 结果与问题无关 / 分数偏低 / 命中 0 时，先放宽 strictness：strict → normal → loose；
    2. 仍不理想则改写 query，用更接近原文可能出现的措辞；
    3. 同一问题最多重试 2 次。仍无结果就如实告知用户知识库中没有相关内容，不要编造。
    """
    query: str = Field(
        description="要检索的问题或关键词。首次用原问题；重试时可改写措辞"
    )
    strictness: Literal["strict", "normal", "loose"] = Field(
        default="normal",
        description="最终重排分数过滤：strict 严格，normal 平衡，loose 放宽；不会提前过滤两路召回",
    )
    breadth: Literal["narrow", "normal", "wide"] = Field(
        default="normal",
        description="检索广度：narrow 少而精，normal 平衡，wide 取更多片段",
    )
    source: str = Field(default='', description='留空使用默认知识库；显式 @名称 使用 TOOL_ROOTS 配置的只读目录')


@register_tool(RagSearchArgs, name="rag_search", concurrency="read")
def rag_search(query: str, strictness: str = "normal", breadth: str = "normal", source: str = '') -> str:
    """
    从本地知识库中检索与问题最相关的文本片段。
    """
    if source:
        try:
            if not source.startswith('@'):
                raise SandboxError('请使用已配置的 @名称，只接受命名根，不接受任意路径')
            root = read_only_root(source[1:])
        except SandboxError as exc:
            audit('blocked', action='rag_search', source=source, reason=str(exc))
            return f'[已拦截] {exc}'
        audit('read', action='rag_search', source=source)
        result = _rag_search(query, strictness=strictness, breadth=breadth, index=get_index(root=root))
    else:
        result = _rag_search(query, strictness=strictness, breadth=breadth)
    logger.debug("RAG 返回 %d 字符", len(result))
    return result
