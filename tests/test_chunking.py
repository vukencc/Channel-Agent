"""rag/chunking.py 父子分块测试（纯函数，不需要模型或网络）。"""
from ai_agent_startup.rag.chunking import TextSplitter


def test_parent_split_by_blank_line_and_offsets_align():
    splitter = TextSplitter()
    text = "第一段第一句。第一段第二句！\n\n第二段。"
    parents = splitter.split_parents(text)

    assert [p[0] for p in parents] == ["第一段第一句。第一段第二句！", "第二段。"]
    for piece, start, end in parents:
        assert text[start:end] == piece          # 范围与内容一致


def test_child_split_by_sentence_and_offsets_align():
    splitter = TextSplitter()
    parent = "第一句。第二句！第三句？"
    children = splitter.split_children(parent)

    assert [c[0] for c in children] == ["第一句。", "第二句！", "第三句？"]
    for piece, start, end in children:
        assert parent[start:end] == piece


def test_child_split_handles_english_and_unterminated_tail():
    splitter = TextSplitter()
    children = splitter.split_children("Hello world. How are you? I am fine")
    assert [c[0] for c in children] == ["Hello world.", "How are you?", "I am fine"]


def test_child_split_does_not_break_decimals():
    splitter = TextSplitter()
    children = splitter.split_children("得分是 0.36 分。下一个。")
    assert [c[0] for c in children] == ["得分是 0.36 分。", "下一个。"]


def test_empty_text_yields_nothing():
    splitter = TextSplitter()
    assert splitter.split_parents("   \n\n  ") == []
    assert splitter.split_children("   ") == []
