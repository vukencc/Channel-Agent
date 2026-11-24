"""验证正式回执的标准输出边界及业务失败。"""
import json

import pytest

from agent_core.core.custom_tool_results import custom_result_outcome
from agent_core.core.flow_handoff_receipts import FlowHandoffReceipts
from agent_core.core.instrumentation import tool_outcome


PREFIX = "[退出码] 0\n[stdout]\n"
CONTRACT = {"fields": {"submitted_ticket_ids": {"type": "array"}}}


def observe(receipts: FlowHandoffReceipts, call_id: str, result: str) -> None:
    name = "submit_to_ops"
    call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
    metrics = {"name": name, "tool_call_id": call_id, "action_stage": "commit", "source": "executed",
               "outcome": "completed", "execution_progress": True}
    receipts.observe(call, result, metrics)


def test_complete_second_stdout_receipt_counts_when_stderr_has_diagnostics():
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "first", PREFIX + '{"ok":true,"ticket":{"ticket_id":"T1"}}')
    observe(receipts, "second", PREFIX + '{"ok":true,"ticket":{"feedback_id":"F2"}}'
            + '\n[stderr]\n收集诊断\n{"ticket_id":"STDERR-ONLY"}')
    assert receipts.expectations() == {"submitted_ticket_ids": ["T1"]}
    assert receipts.review_facts()[0]["completed_count"] == 2
    assert "F2" in receipts.guide(include_values=True)
    assert "STDERR-ONLY" not in receipts.guide(include_values=True)


@pytest.mark.parametrize("marker", ["[输出已截断]", "已截断）"])
def test_literal_marker_in_business_error_does_not_hide_failure(marker):
    output = PREFIX + json.dumps({"ok": False, "error": f"源文件包含字面文本 {marker}"}, ensure_ascii=False)
    assert custom_result_outcome(output) == "business_error"
    assert tool_outcome(output, custom_tool=True) == "error"


@pytest.mark.parametrize("stdout", [
    '{"ok":true,"ticket":{"ticket_id":"T2"}}\n[输出已截断]',
    '{"ok":true,"ticket":{"ticket_id":"T2"}}\n…（输出超过 50 字符，已截断）',
    '{"ok":true,"ticket":{"ticket_id":"T2"}}\n{}',
    '{"ok":true,"ticket":{"ticket_id":"T2","ticket_id":"T3"}}',
    '{"ok":true,"ticket":{"ticket_id":"T2"}',
    '{"ok":true,"ticket":{}',
])
def test_truncated_or_ambiguous_stdout_stays_unknown_with_complete_stderr(stdout):
    output = PREFIX + stdout + '\n[stderr]\n{"ok":true,"ticket":{"ticket_id":"T3"}}'
    assert custom_result_outcome(output) == "unknown"
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "first", PREFIX + '{"ok":true,"ticket":{"ticket_id":"T1"}}')
    observe(receipts, "second", output)
    assert receipts.expectations() == {}


@pytest.mark.parametrize("result", [
    '[退出码] 2\n[stdout]\n{"ok":true,"ticket":{"ticket_id":"T2"}}',
    PREFIX + '{"ok":false,"error":"办理失败","ticket":{"ticket_id":"T2"}}',
    PREFIX + '{"ok":true,"success":false,"ticket":{"ticket_id":"T2"}}',
    PREFIX + '{"ok":true,"status":"failed","ticket":{"ticket_id":"T2"}}',
    PREFIX + '{"ok":true,"ticket":{}}\n[stderr]\n{"ticket_id":"T2"}',
])
def test_nonzero_failure_or_stderr_only_id_cannot_extend_receipts(result):
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "first", PREFIX + '{"ok":true,"ticket":{"ticket_id":"T1"}}')
    observe(receipts, "second", result)
    assert receipts.expectations() == {}
