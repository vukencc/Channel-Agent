"""RAG 检索工具：从本地知识库检索相关片段。"""
from typing import Literal

from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, Field, field_validator

from ai_agent_startup import config

from ai_agent_startup.rag.tool import rag_search as _rag_search
from ai_agent_startup.tools.base import register_tool
from ai_agent_startup.core.log import get_logger
from ai_agent_startup.rag.index import get_index
from ai_agent_startup.tools.sandbox import read_only_root, SandboxError, audit

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
    source: str = Field(default='', description='留空使用默认知识库；名称选择 RAG_SOURCES 知识库；@名称 选择 TOOL_ROOTS 只读目录')
    top_k: int | None = Field(default=None, ge=1, le=config.RAG_MAX_TOP_K, strict=True,
                             description='显式返回数量上限，留空按 breadth 决定')
    updated_after: datetime | None = Field(default=None, description='仅检索此 ISO 8601 时间之后修改的文件，必须带时区，例如 2026-01-01T00:00:00Z')

    @field_validator('updated_after')
    @classmethod
    def require_timezone(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('updated_after 必须带时区')
        return value


@register_tool(RagSearchArgs, name="rag_search", concurrency="read")
def rag_search(query: str, strictness: str = "normal", breadth: str = "normal", source: str = '',
               top_k: int | None = None, updated_after: datetime | None = None) -> str:
    """
    从本地知识库中检索与问题最相关的文本片段。
    """
    options = {}
    if top_k is not None:
        if type(top_k) is not int or not 1 <= top_k <= config.RAG_MAX_TOP_K:
            raise ValueError('top_k 超出允许范围')
        options['top_k'] = top_k
    if updated_after is not None:
        if updated_after.tzinfo is None:
            raise ValueError('updated_after 必须带时区')
        options['updated_after'] = updated_after.timestamp()
    if source:
        try:
            if source.startswith('@'):
                root = read_only_root(source[1:])
            elif source in config.RAG_SOURCES:
                entry = config.RAG_SOURCES[source]
                root = Path(entry['path']).absolute()
                if root.resolve() != root or not root.is_dir():
                    raise SandboxError('知识库根不存在或路径发生变化')
                if 'thresholds' in entry:
                    options['thresholds'] = entry['thresholds']
            else:
                raise SandboxError('未知知识库；只接受已配置名称，不接受任意路径')
        except SandboxError as exc:
            audit('blocked', action='rag_search', source=source, reason=str(exc))
            return f'[已拦截] {exc}'
        audit('read', action='rag_search', source=source)
        index_options = {'track_updates': True} if updated_after is not None else {}
        result = _rag_search(query, strictness=strictness, breadth=breadth, index=get_index(root=root, **index_options), **options)
    else:
        result = _rag_search(query, strictness=strictness, breadth=breadth, **options)
    logger.debug("RAG 返回 %d 字符", len(result))
    return result
