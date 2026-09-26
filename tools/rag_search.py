"""RAG 检索工具：从本地知识库检索相关片段。"""
from typing import Literal

from pydantic import BaseModel, Field

from rag.tool import rag_search as _rag_search
from tools.base import register_tool
import config


class RagSearchArgs(BaseModel):
    """
    从本地知识库中检索与问题最相关的文本片段。当需要回答关于知识库内容的问题时调用。

    返回内容自带质量信息（命中数量、相似度阈值、每条分数），请据此判断是否需要重试：
    1. 结果与问题无关 / 分数偏低 / 命中 0 时，先放宽 strictness：strict → normal → loose；
    2. 仍不理想则改写 query，用更接近原文可能出现的措辞；
    3. 同一问题最多重试 2 次。仍无结果就如实告知用户知识库中没有相关内容，不要编造。
    """
    query: str = Field(
        description="要检索的问题或关键词。首次用原问题；重试时可改写措辞"
    )
    strictness: Literal["strict", "normal", "loose"] = Field(
        default="normal",
        description="匹配严格度：strict 只取最相关的，normal 平衡，loose 放宽阈值以多召回",
    )
    breadth: Literal["narrow", "normal", "wide"] = Field(
        default="normal",
        description="检索广度：narrow 少而精，normal 平衡，wide 取更多片段",
    )


@register_tool(RagSearchArgs, name="rag_search")
def rag_search(query: str, strictness: str = "normal", breadth: str = "normal") -> str:
    """
    从本地知识库中检索与问题最相关的文本片段。
    """
    result = _rag_search(query, strictness=strictness, breadth=breadth)
    if config.DEBUG:
        print(result)
    return result
