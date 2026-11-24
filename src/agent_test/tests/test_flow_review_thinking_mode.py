"""通过真实 SDK 请求组装与离线 HTTP 传输核对 Flow 独立复核的参数范围。"""
import asyncio
import json
import runpy

import httpx
import pytest
from openai import AsyncOpenAI

from agent_core import config
from agent_core.core import flow_draft_gate, llm
from agent_core.core.flow_draft_gate import FlowDraftGate
from agent_core.core.model_settings import (active_model_fallbacks, active_reasoning_effort,
                                            active_thinking_mode, model_thinking)
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def _stream_reply(text: str) -> httpx.Response:
    chunk = {"id": "controlled", "object": "chat.completion.chunk", "created": 0,
             "model": "controlled-model", "choices": [
                 {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"},
             ]}
    content = chr(10).join(("data: " + json.dumps(chunk, ensure_ascii=False), "", "data: [DONE]", "", ""))
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)


@pytest.mark.parametrize("stage", ["handoff", "preview", "tool"])
@pytest.mark.parametrize("mode,outcome", [
    ("inherit", "pass"), ("disabled", "pass"), ("disabled", "recover"), ("disabled", "unsupported"),
])
def test_flow_source_review_thinking_reaches_provider_and_is_request_scoped(tmp_path, monkeypatch, stage, mode, outcome):
    monkeypatch.setattr(config, "FLOW_REVIEW_THINKING_MODE", mode)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 1)
    monkeypatch.setattr(config, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(flow_draft_gate, "review_sources", lambda *args, **kwargs: [
        {"id": "S1", "kind": "read_file", "text": "页面无法提交，请帮忙排查。"},
    ])
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    requests = []
    review_requests = []

    async def run():
        review_waiting = asyncio.Event()
        allow_review = asyncio.Event()

        async def transport(request):
            data = json.loads(request.content)
            review = "独立审查员" in data["messages"][0]["content"]
            requests.append((review, str(request.url), data, active_model_fallbacks.get()))
            if review:
                review_requests.append(data)
                if len(review_requests) == 1:
                    review_waiting.set()
                    await allow_review.wait()
                    if outcome == "unsupported":
                        return httpx.Response(400, json={"error": {"message": "thinking unsupported"}})
                    if outcome == "recover":
                        return _stream_reply('{"status":"revise","issues":[{}]}')
                return _stream_reply('{"status":"pass"}')
            return _stream_reply("原请求正常回应。")

        async def client(endpoint):
            return AsyncOpenAI(api_key="fixture", base_url=endpoint["base_url"], max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))

        monkeypatch.setattr(llm, "_runtime_client", client)
        manager = SessionManager(store)
        try:
            profile = manager.model_registry.add(name="受控模型", model="controlled-model",
                                                 base_url="http://127.0.0.1:9/v1")
            session = manager.create()
            session.record.update(model_profile=profile["id"], reasoning_effort="medium", thinking_mode="auto",
                                  last_run={"turn_id": "review-fixture"})
            gate = FlowDraftGate(manager, session, lambda: True)

            async def independent_review():
                with model_thinking("low", "enabled"):
                    feedback = await gate.check("客户明确要求人工处理。", stage=stage)
                    assert active_reasoning_effort.get() == "low" and active_thinking_mode.get() == "enabled"
                    return feedback

            task = asyncio.create_task(independent_review())
            try:
                await asyncio.wait_for(review_waiting.wait(), 2)
                # 与复核并行的主请求，以及后续无工具请求，均使用节点自己的设置。
                await manager._call_model([{"role": "user", "content": "主业务"}], session_id=session.id)
            finally:
                allow_review.set()
                feedback = await task
            await manager._call_model([{"role": "user", "content": "普通格式修订"}],
                                      session_id=session.id, disable_tools=True)
            assert session.record["reasoning_effort"] == "medium" and session.record["thinking_mode"] == "auto"
            if outcome == "unsupported":
                assert feedback and gate.blocked
                event = session.record["last_run"]["draft_reviews"][-1]
                assert event["status"] == "RuntimeModelRequestError"
                assert len(review_requests) == 1
            else:
                assert feedback is None and not gate.blocked
                assert len(review_requests) == (2 if outcome == "recover" else 1)
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()

    assert {url for _, url, _, _ in requests} == {"http://127.0.0.1:9/v1/chat/completions"}
    assert all(body["model"] == "controlled-model" for _, _, body, _ in requests)
    for is_review, _, body, fallback_allowed in requests:
        if is_review and mode == "disabled":
            assert body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body
            assert not fallback_allowed
        else:
            assert "thinking" not in body and body["reasoning_effort"] == "medium"
            assert fallback_allowed



@pytest.mark.parametrize("value, expected", [(None, "inherit"), ("inherit", "inherit"), (" Disabled ", "disabled")])
def test_review_thinking_mode_configuration(monkeypatch, value, expected):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    if value is None:
        monkeypatch.delenv("FLOW_REVIEW_THINKING_MODE", raising=False)
    else:
        monkeypatch.setenv("FLOW_REVIEW_THINKING_MODE", value)
    assert runpy.run_path(config.__file__)["FLOW_REVIEW_THINKING_MODE"] == expected


@pytest.mark.parametrize("value", ["auto", "enabled", "unexpected"])
def test_review_thinking_mode_rejects_unsupported_values(monkeypatch, value):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("FLOW_REVIEW_THINKING_MODE", value)
    with pytest.raises(ValueError, match="FLOW_REVIEW_THINKING_MODE"):
        runpy.run_path(config.__file__)
