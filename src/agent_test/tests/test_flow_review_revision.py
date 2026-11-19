import json
import pytest

from agent_core.core.flow_review_revision import revision_targets


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def test_revised_record_and_other_new_field_are_reviewed():
    old = {"id": "T1", "customer": "客户要求立刻操作。", "policy": "必须先审批。", "count": 1}
    new = {"id": "T1", "customer": "客户未要求操作。", "policy": "只有审批后才能操作。", "count": 1, "note": "未经核实。"}
    issue = {"id": "C1", "field": "/customer", "start": 0, "end": len(old["customer"]), "text": old["customer"]}
    result = revision_targets(dump(old), [issue], dump(new))
    assert result is not None
    assert {r["field"]: r["text"] for r in result} == {"/customer": new["customer"], "/policy": new["policy"], "/note": new["note"]}
    assert all(new[r["field"][1:]][r["start"]:r["end"]] == r["text"] for r in result)


def test_unchanged_issue_and_escaped_pointer():
    old = {"a/b~c": "前文。\n不得操作。后文。", "other": "保持"}
    text = "不得操作。"
    start = old["a/b~c"].index(text)
    issue = {"id": "C1", "field": "/a~1b~0c", "start": start, "end": start + len(text), "text": text}
    result = revision_targets(dump(old), [issue], dump(old))
    assert result and result[0]["text"] == old["a/b~c"] and result[0]["start"] == 0


def test_uncertain_changes_fall_back():
    old = {"id": "T1", "text": "必须审批。", "n": 1}
    issue = {"id": "C1", "field": "/text", "start": 0, "end": 5, "text": "必须审批。"}
    for current in ({**old, "text": ""}, {**old, "n": 2}, {**old, "id": "T2"},
                    {**old, "text": "上述内容有效。"}, {"id": "T1", "n": 1},
                    {**old, "new": {"x": "值"}}):
        assert revision_targets(dump(old), [issue], dump(current)) is None
    assert revision_targets(dump(old), [issue], '{"id":"T1","id":"T2"}') is None
    assert revision_targets(dump(old), [{**issue, "end": 4}], dump(old)) is None
    assert revision_targets(dump(old), [issue, issue], dump(old)) is None


def test_nested_records_condition_and_structure_boundaries():
    old = {"items": [{"id": "T1", "text": "允许操作。", "detail": "原文"}]}
    new = {"items": [{"id": "T1", "text": "只有审批后，才能操作；否则不得操作。\n例外需复核。", "detail": "改写"}]}
    issue = {"id": "C1", "field": "/items/0/text", "start": 0, "end": 5, "text": "允许操作。"}
    result = revision_targets(dump(old), [issue], dump(new))
    assert result and {p["field"]: p["text"] for p in result} == {
        "/items/0/text": new["items"][0]["text"], "/items/0/detail": "改写"}
    assert revision_targets(dump(old), [issue], dump({"items": []})) is None
    assert revision_targets(dump(old), [{**issue, "field": "/items/00/text"}], dump(new)) is None
    assert revision_targets(dump(old), [issue], dump({"items": [{"id": "T1", "text": "x" * 4001, "detail": "原文"}]})) is None


def test_sentence_changes_and_old_issue_are_all_retained():
    old = {"id": "T1", "text": "第一句。旧中句。第三句。", "other": "甲句。乙句。丙句。"}
    new = {"id": "T1", "text": "第一句。新中句。第三句。", "other": "甲句。新乙句。丙句。新增句。"}
    issue = {"id": "C1", "field": "/text", "start": 4, "end": 8, "text": "旧中句。"}
    result = revision_targets(dump(old), [issue], dump(new))
    assert result and [(r["field"], r["text"]) for r in result] == [
        ("/other", "新乙句。"), ("/other", "新增句。"), ("/text", "新中句。")]
    for row in result:
        assert new[row["field"][1:]][row["start"]:row["end"]] == row["text"]
    unchanged_issue = {**issue, "start": 0, "end": 4, "text": "第一句。"}
    result = revision_targets(dump(old), [unchanged_issue], dump(new))
    assert result and [r["text"] for r in result if r["field"] == "/text"] == ["第一句。", "新中句。"]


def test_sentence_deletion_and_condition_use_full_field():
    old = {"text": "第一句。第二句。第三句。"}
    issue = {"id": "C1", "field": "/text", "start": 4, "end": 8, "text": "第二句。"}
    for text in ("第一句。第三句。", "第一句。只有审批后才能操作。第三句。"):
        result = revision_targets(dump(old), [issue], dump({"text": text}))
        assert result and result[0]["text"] == text


@pytest.mark.parametrize("stage,change", [("tool", "none"), ("preview", "none"),
                                          ("tool", "source"), ("tool", "definition"),
                                          ("preview", "model"), ("preview", "turn"), ("preview", "policy")])
def test_gate_focused_recheck_after_verified_revise(monkeypatch, stage, change):
    import asyncio
    from types import SimpleNamespace
    from agent_core.core.flow_draft_gate import FlowDraftGate
    import agent_core.core.flow_draft_gate as gate_module
    import agent_core.core.flow_review_items as items_module

    sources = [{"id": "S1", "kind": "read_file", "scope": "observed_span", "text": "客户表示原始情况。"}]
    monkeypatch.setattr(gate_module, "review_sources", lambda *a, **k: sources)
    monkeypatch.setattr(items_module, "tool_review_sources", lambda draft, rows: rows)
    histories = []
    class Manager:
        def _observe_phase(self, *args): pass
        def notify(self): pass
        def emit(self, *args): pass
        async def _call_model(self, history, **kwargs):
            histories.append(json.loads(history[1]["content"]))
            if len(histories) == 1:
                target = histories[-1]["targets"][1]
                return {"role": "assistant", "content": json.dumps({"status": "revise", "issues": [{
                    "target_id": target["id"], "draft_quote": "客户表示旧情况。", "source_id": "S1",
                    "quote": "客户表示原始情况。", "reason": "与原文不符"}]}, ensure_ascii=False)}
            return {"role": "assistant", "content": '{"status":"pass"}'}
    session = SimpleNamespace(record={"last_run": {}}, flow_review_task="复核", phase="运行", id="s",
                              cancelled=asyncio.Event(), last_model_event_at=0)
    gate = FlowDraftGate(Manager(), session, lambda: True)
    old = dump({"text": "客户表示背景情况。客户表示旧情况。客户表示其他情况。"})
    new = dump({"text": "客户表示背景情况。客户表示新情况。客户表示其他情况。"})
    async def run():
        assert await gate.check(old, stage=stage, force=True, revision_key=(stage, "T1"), revision_definition={"name": "tool"})
        if change == "source":
            sources[0]["text"] = "客户表示新的来源情况。"
        if change == "model":
            session.record["model_profile"] = "new-profile"
        if change == "turn":
            session.record["last_run"]["turn_id"] = "another-turn"
        if change == "policy":
            session.record["permission_policy"] = "read_only"
        definition = {"name": "changed"} if change == "definition" else {"name": "tool"}
        assert await gate.check(new, stage=stage, force=True, revision_key=(stage, "T1"), revision_definition=definition) is None
    asyncio.run(run())
    expected = ["客户表示新情况。"] if change == "none" else ["客户表示背景情况。", "客户表示新情况。", "客户表示其他情况。"]
    assert [row["excerpt"] for row in histories[1]["targets"]] == expected
    assert json.loads(histories[1]["draft"]) == json.loads(new)
    assert histories[1]["sources"] == sources

    if change == "none":
        current = histories[1]
        assert list(current)[:2] == ["targets", "revision_context"]
        assert current["revision_context"]["source_locations"] == [{
            "field": "/text", "source_id": "S1", "start": 0, "end": len("客户表示原始情况。"),
            "quote": "客户表示原始情况。"}]
        assert current["revision_context"]["edits"][0]["field"] == "/text"
        assert current["targets"][0]["excerpt"] == "客户表示新情况。"
        assert "claim_texts" not in current
    else:
        assert "revision_context" not in histories[1]
