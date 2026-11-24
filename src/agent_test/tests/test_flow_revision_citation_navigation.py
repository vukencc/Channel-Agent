import json

from agent_core.core import flow_review_revision as revision


def target(field, text, start=0, end=None, **extra):
    end = len(text) if end is None else end
    return {"id": "C1", "field": field, "start": start, "end": end,
            "text": text[start:end], "context": text, **extra}


def test_navigation_preserves_verified_locations_and_reports_edits():
    old = {"items": [{"body": "旧内容。"}], "secret": "不应传出"}
    new = {"items": [{"body": "新内容。", "added": "新字段"}], "secret": "不应传出"}
    spans = [{"source_id": "S", "start": 0, "end": 2, "quote": "来源"},
             {"source_id": "S", "start": 3, "end": 5, "quote": "来源"}]
    result = revision.revision_navigation(json.dumps(old), [target("/items/0/body", "旧内容。", _cited_spans=spans)],
        json.dumps(new), [target("/items/0/body", "新内容。"), target("/items/0/added", "新字段")],
        [{"id": "S", "text": "来源，来源"}])
    assert [x["start"] for x in result["source_locations"]] == [0, 3]
    assert {x["field"] for x in result["edits"]} == {"/items/0/body", "/items/0/added"}
    assert "不应传出" not in str(result)


def test_navigation_rejects_unverified_or_mismatched_locations():
    draft = json.dumps({"a": ["原文"]})
    old = target("/a/0", "原文", _cited_spans=[{"source_id": "S", "start": 0, "end": 2, "quote": "来源"}])
    current = target("/a/0", "原文")
    for sources in ([{"id": "S", "text": "篡改"}], [{"id": "S", "text": "来源"}, {"id": "S", "text": "来源"}]):
        assert revision.revision_navigation(draft, [old], draft, [current], sources) == {}
    assert revision.revision_navigation(draft, [old], draft, [target("/a/00", "原文")], [{"id": "S", "text": "来源"}]) == {}
    old["_cited_spans"][0]["end"] = 99
    assert revision.revision_navigation(draft, [old], draft, [current], [{"id": "S", "text": "来源"}]) == {}


def test_navigation_limits_and_unchanged_dispute():
    draft = json.dumps({"a": "原文"})
    spans = [{"source_id": "S", "start": i, "end": i + 1, "quote": "字"} for i in range(7)]
    spans.append(spans[0])
    result = revision.revision_navigation(draft, [target("/a", "原文", _cited_spans=spans)], draft,
        [target("/a", "原文")], [{"id": "S", "text": "字" * 7}])
    assert len(result["source_locations"]) == 5
    assert result["edits"] == []


def test_navigation_uses_field_not_old_target_number_and_validates_both_drafts():
    old = json.dumps({"a/b~c": "旧句", "other": "另一句"})
    new = json.dumps({"a/b~c": "新句", "other": "另一句"})
    span = {"source_id": "S", "start": 0, "end": 2, "quote": "证据"}
    prior = target("/a~1b~0c", "旧句", _cited_spans=[span])
    current = target("/a~1b~0c", "新句")
    current["id"] = "C99"
    result = revision.revision_navigation(old, [prior], new, [current], [{"id": "S", "text": "证据"}])
    assert result["source_locations"][0]["field"] == "/a~1b~0c"
    assert result["edits"] == [{"field": "/a~1b~0c", "start": 0, "end": 1, "old_text": "旧", "new_text": "新"}]
    prior["text"] = "伪造"
    assert revision.revision_navigation(old, [prior], new, [current], [{"id": "S", "text": "证据"}]) == {}


def test_navigation_long_changes_omit_text_and_long_citations_are_excluded():
    old_text, new_text = "旧" * 200, "新" * 200
    span = {"source_id": "S", "start": 0, "end": 1, "quote": "证"}
    result = revision.revision_navigation(json.dumps({"a": old_text}),
        [target("/a", old_text, _cited_spans=[span])], json.dumps({"a": new_text}),
        [target("/a", new_text)], [{"id": "S", "text": "证"}])
    assert result["edits"] == [{"field": "/a", "start": 0, "end": 200}]
    span.update(end=161, quote="证" * 161)
    assert revision.revision_navigation(json.dumps({"a": old_text}),
        [target("/a", old_text, _cited_spans=[span])], json.dumps({"a": new_text}),
        [target("/a", new_text)], [{"id": "S", "text": "证" * 161}]) == {}


def test_navigation_never_exposes_unrelated_old_text():
    old = {"a": "争议旧句。秘密旧值", "b": "删除的敏感文本"}
    new = {"a": "争议新句。新值", "b": "替换内容"}
    span = {"source_id": "S", "start": 0, "end": 2, "quote": "证据"}
    prior = target("/a", old["a"], 0, 5, _cited_spans=[span])
    result = revision.revision_navigation(json.dumps(old), [prior], json.dumps(new),
        [target("/a", new["a"]), target("/b", new["b"])], [{"id": "S", "text": "证据"}])
    assert result["source_locations"]
    assert "秘密旧值" not in str(result)
    assert "删除的敏感文本" not in str(result)
    assert all("old_text" not in edit for edit in result["edits"])


def test_navigation_only_shows_old_diff_inside_verified_target():
    old, new = {"a": "争议旧句。其它"}, {"a": "争议新句。其它"}
    span = {"source_id": "S", "start": 0, "end": 2, "quote": "证据"}
    prior = target("/a", old["a"], 0, 5, _cited_spans=[span])
    result = revision.revision_navigation(json.dumps(old), [prior], json.dumps(new),
        [target("/a", new["a"])], [{"id": "S", "text": "证据"}])
    assert result["edits"][0]["old_text"] == "旧"
