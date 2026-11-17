import asyncio
from pathlib import Path

import pytest

from agent_core.core.flow_designer import design_flow
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.mark.parametrize("instruction", [
    "声称外部动作已完成须有权威回执；本地模拟动作须明确标为模拟",
    "来源自述的事实须保留并标明来源及核实状态，不得因尚未核实而称未提供",
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
