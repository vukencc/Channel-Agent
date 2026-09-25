from typing import Literal

from pydantic import BaseModel, Field
from tavily import TavilyClient

import config
from tools.base import register_tool

tavily_client = TavilyClient(api_key=config.WEB_SEARCH_API_KEY)


class WebSearchArgs(BaseModel):
    """
    进行联网搜索，返回相关网页的标题、链接和摘要。当需要实时信息、新闻或最新数据时调用。
    """
    query: str = Field(description="搜索关键词")
    max_results: int = Field(default=5, description="返回结果数量，默认 5")
    search_depth: Literal["basic", "advanced"] = Field(
        default="basic", description="搜索深度，basic 更快，advanced 更全"
    )


@register_tool(WebSearchArgs, name="web_search")
def web_search(query: str, max_results: int = 5, search_depth: str = "basic") -> str:
    """
    进行联网搜索，返回相关网页的标题、链接和摘要。
    当你需要获取实时信息、新闻或最新数据时，调用此工具。
    """
    response = tavily_client.search(
        query=query,
        max_results=max_results,
        search_depth=search_depth, # type: ignore
    )

    return "\n".join([
        f"- [{r['title']}]({r['url']}): {r['content']}"
        for r in response.get("results", [])
    ])
