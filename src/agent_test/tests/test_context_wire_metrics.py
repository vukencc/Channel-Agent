"""上下文估算应排除不会发送的顶层运行时字段，保留业务值与图片预算。"""
import pytest

from agent_core.core import context


@pytest.mark.parametrize("private_key", [
    "_context_observation", "_provenance", "_context_reference",
])
def test_request_metrics_ignore_private_runtime_fields(private_key):
    public = [{"role": "user", "content": '{"_business":"必须保留"}'},
              {"role": "assistant", "content": "原回答", "reasoning_content": "保留推理"}]
    private = [dict(message, **{private_key: "运行时数据" * 2000}) for message in public]
    schemas = [{"type": "function", "function": {"name": "read_file"}}]
    assert context.request_tokens(private, schemas) == context.request_tokens(public, schemas)
    assert context.history_size(private) == context.history_size(public)
    assert context.request_tokens(public, schemas) > context.request_tokens(public)


def test_request_metrics_preserve_attachment_and_nested_business_values(monkeypatch):
    monkeypatch.setattr(context, "attachment_tokens",
                        lambda message: 100 if message.get("_attachments") else 0)
    message = {"role": "user", "content": [{"type": "text", "text": "_business"}],
               "_attachments": [{"id": "local-image"}]}
    private = {**message, "_context_observation": {"sha256": "0" * 64}}
    assert context.request_tokens([private]) == context.request_tokens([message])
    without_image = {key: value for key, value in message.items() if key != "_attachments"}
    assert context.request_tokens([private]) >= context.request_tokens([without_image]) + 100
    changed = {**without_image, "content": [{"type": "text", "text": "_business" * 20}]}
    assert context.request_tokens([changed]) > context.request_tokens([without_image])
