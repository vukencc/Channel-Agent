import asyncio

from ai_agent_startup import config
from ai_agent_startup.rag.assess import assess_rag


def test_huge_assessment_input_is_bounded(monkeypatch):
    monkeypatch.setattr(config, 'ASSESS_INPUT_CHARS', 2000, raising=False)
    monkeypatch.setattr(config, 'ASSESS_CONTEXT_CHARS', 200, raising=False)
    seen = []
    async def judge(prompt):
        seen.append(prompt)
        assert len(prompt) <= 2000
        assert '100' in prompt and '截断' in prompt
        return '评估完成'
    result = asyncio.run(assess_rag('问题' * 10000, ['片段' * 10000] * 100, '答案' * 10000, judge))
    assert result == '评估完成' and len(seen) == 1
