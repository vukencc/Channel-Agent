import asyncio
from pathlib import Path

import pytest

from agent_core.core.flow_designer import design_flow
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.mark.parametrize("instruction", [
    "声称外部动作已完成须有权威回执；本地模拟动作须明确标为模拟",
    "来源自述的事实须保留并标明来源及核实状态，不得因尚未核实而称未提供",
    "已报告与未提供的分类不得互相矛盾；自述但未核实不是未提供，缺口须指出具体未报告的子字段或来源",
    "来源字段标签和清单须保留原意，不得替换成不同含义",
    "交接中声称已尝试或已检索须对应同一事项的实际调用及结果，不得借用其他事项或次数",
    "列入未解决或转交前，须将每个缺口关联到原需求中尚未满足的部分；未被要求的辅助信息未知不自动产生新任务",
])
def test_designer_sends_evidence_wording_contract(tmp_path: Path, instruction: str) -> None:
    seen = []

    async def model(history, *, session_id, emit):
        seen.append(history)
        return {"role": "assistant", "content": "{\"reply\":\"请补充需求\",\"template\":null}"}

    messages = [{"role": "user", "content": "设计包含外部动作和事实摘要的流程"}]
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    try:
        result = asyncio.run(design_flow(SessionManager(store, model=model), messages))
        assert seen[0][1:] == messages
        assert instruction in seen[0][0]["content"]
        assert result["template"] is None
    finally:
        store.close()
