"""基于以下维度进行 RAG 评估：

    Hit Rate -> 检索到文档的成功率
    MRR      -> 1/第一个相关文档的位置
    精确率   -> 检索到的相关文档数 / 检索到的文档总数
    覆盖率   -> 召回率，即检索到的相关文档数 / 总相关文档数
    忠诚度   -> 检查答案中的事实是否都能在上下文中找到依据
    相关性   -> 评估答案与查询的相关性

分工：
- 检索指标（前四个）需要「哪些文档算相关」的真值，以纯函数形式提供，
  供离线按标注集精确计算。
- 运行时的 RAG 评估由 LLM 统一负责：assess_rag 把【问题】【检索到的上下文】
  【模型答案】一起交给裁判模型，一次产出对上述维度的评估结果。
"""
from typing import Awaitable, Callable, Iterable, Sequence

# 裁判函数：接收提示词，返回模型文本回复（通常传入 core.llm.complete）
Judge = Callable[[str], Awaitable[str]]


# ------------------------------------------------------------------
# 检索指标（纯函数）：输入检索到的文档 id 列表 + 真值（相关文档 id 集合）
# ------------------------------------------------------------------

def hit_rate(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    """检索到文档的成功率：检索结果里只要出现相关文档即命中，返回 1.0 / 0.0。"""
    rel = set(relevant)
    return 1.0 if any(doc in rel for doc in retrieved) else 0.0


def mrr(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    """1 / 第一个相关文档的位置；没有相关文档时返回 0.0。"""
    rel = set(relevant)
    for rank, doc in enumerate(retrieved, start=1):
        if doc in rel:
            return 1.0 / rank
    return 0.0


def precision(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    """精确率：检索到的相关文档数 / 检索到的文档总数。"""
    if not retrieved:
        return 0.0
    rel = set(relevant)
    return sum(1 for doc in retrieved if doc in rel) / len(retrieved)


def recall(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    """覆盖率（召回率）：检索到的相关文档数 / 总相关文档数。"""
    rel = set(relevant)
    if not rel:
        return 0.0
    got = set(retrieved)
    return sum(1 for doc in rel if doc in got) / len(rel)


# ------------------------------------------------------------------
# 运行时评估：由 LLM 统一负责
# ------------------------------------------------------------------

_ASSESS_PROMPT = """你是严谨的 RAG 评估员。请对下面这一次「检索增强生成」做整体评估。

【问题】
{query}

【检索到的上下文】
{contexts}

【模型答案】
{answer}

请对以下 6 个维度各给一个 0~1 的分数（保留两位小数）：
1. Hit Rate：检索到的上下文里是否包含回答该问题所需的信息
2. MRR：最相关的那段上下文排在第几位（用 1/排名 表示）
3. 精确率：检索到的上下文中与问题相关的比例
4. 覆盖率：回答该问题所需的信息被检索到的比例
5. 忠诚度：答案中的事实是否都能在上下文中找到依据
6. 相关性：答案是否切题

只输出评估结果，不要解释过程，格式如下：
Hit Rate: <分数>
MRR: <分数>
精确率: <分数>
覆盖率: <分数>
忠诚度: <分数>
相关性: <分数>
结论: <一句话>"""


async def assess_rag(query: str, contexts: Sequence[str], answer: str, judge: Judge) -> str:
    """由 LLM 统一对一轮 RAG 做评估，返回评估结果文本。"""
    prompt = _ASSESS_PROMPT.format(
        query=query,
        contexts="\n---\n".join(contexts),
        answer=answer,
    )
    return (await judge(prompt)).strip()
