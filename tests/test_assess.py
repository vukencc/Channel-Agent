"""rag.assess 指标测试（纯函数，不需要网络或模型）。"""
import asyncio

from rag.assess import assess_rag, hit_rate, mrr, precision, recall


def test_hit_rate():
    assert hit_rate(["a#0", "b#0"], ["b#0"]) == 1.0
    assert hit_rate(["a#0", "b#0"], ["c#0"]) == 0.0
    assert hit_rate([], ["a#0"]) == 0.0


def test_mrr():
    assert mrr(["a#0", "b#0"], ["a#0"]) == 1.0        # 第 1 个就命中
    assert mrr(["a#0", "b#0"], ["b#0"]) == 0.5        # 第 2 个命中
    assert mrr(["a#0", "b#0"], ["z#0"]) == 0.0        # 全部不命中


def test_precision():
    assert precision(["a#0", "b#0"], ["a#0", "b#0"]) == 1.0
    assert precision(["a#0", "b#0"], ["a#0"]) == 0.5
    assert precision([], ["a#0"]) == 0.0


def test_recall():
    assert recall(["a#0"], ["a#0", "b#0"]) == 0.5
    assert recall(["a#0", "b#0"], ["a#0", "b#0"]) == 1.0
    assert recall(["a#0"], []) == 0.0


def test_assess_rag_hands_query_context_answer_to_judge_and_returns_its_result():
    """运行时评估由 LLM 统一负责：把问题/上下文/答案交给裁判，并原样返回其结果。"""
    captured: dict[str, str] = {}

    async def fake_judge(prompt: str) -> str:
        captured["prompt"] = prompt
        return "  Hit Rate: 1.00\n结论: 回答有据  "

    result = asyncio.run(
        assess_rag("蓝色花瓶的作用", ["花瓶是固定气枪的支架"], "花瓶是枪架", fake_judge)
    )

    assert result == "Hit Rate: 1.00\n结论: 回答有据"          # 去掉首尾空白
    assert "蓝色花瓶的作用" in captured["prompt"]
    assert "花瓶是固定气枪的支架" in captured["prompt"]
    assert "花瓶是枪架" in captured["prompt"]
