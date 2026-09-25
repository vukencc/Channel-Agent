"""RAG 检索工具：从本地知识库检索相关片段。"""
from pydantic import BaseModel, Field

from rag.tool import rag_search as _rag_search
from tools.base import register_tool
import config

class RagSearchArgs(BaseModel):
    """
    从本地知识库中检索与问题最相关的文本片段。当需要回答关于知识库内容的问题时调用。
    """
    query: str = Field(description="要检索的问题或关键词")
    top_k: int = Field(default=3, description="返回的片段数量，默认 3")


@register_tool(RagSearchArgs, name="rag_search")
def rag_search(query: str, top_k: int = 3) -> str:
    """
    从本地知识库中检索与问题最相关的文本片段。
    """
    if config.DEBUG:
        print(_rag_search(query, top_k))
    return _rag_search(query, top_k)
