import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication
from agent_frontend.desktop.working import WorkingPanel


def test_working_node_and_child_show_effective_tools():
    app = QApplication.instance() or QApplication([])
    panel = WorkingPanel()
    panel.set_runs([{"run_id": "r", "nodes": [{"id": "n", "tools": [
        {"name": "read_file", "description": "读取文件", "required": True, "custom": False},
        {"name": "calculate", "description": "计算结果", "required": False, "custom": True}],
        "subagents": [{"id": "c", "tools": [{"name": "list_files", "description": "列出文件", "required": True, "custom": False}]}]}]}])
    panel._select(panel.listing.item(1))
    detail = panel.details.toPlainText()
    assert "read_file（基础锁定）：读取文件" in detail
    assert "calculate（自定义）：计算结果" in detail
    assert "list_files（基础锁定）：列出文件" in detail
    panel.close()


def test_custom_tool_draft_preserves_invalid_text():
    from agent_frontend.desktop.flow import _custom_tools_draft
    assert _custom_tools_draft("") == []
    invalid = "[{"
    assert _custom_tools_draft(invalid) == invalid
    text = "[{\"name\":\"calc\",\"description\":\"计算\",\"parameters\":{\"type\":\"object\"},\"code\":\"def main(arguments): return 1\",\"network\":false}]"
    assert _custom_tools_draft(text)[0]["name"] == "calc"


def test_flow_custom_tools_selection_and_invalid_draft(monkeypatch):
    from agent_frontend.desktop.flow import FlowPanel
    from PySide6.QtWidgets import QMessageBox
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    panel.set_tool_catalog({"tools": [{"name": "read_file", "required": True}]})
    definition = {"name": "calc", "description": "计算", "parameters": {"type": "object"}, "code": "def main(arguments): return 1", "network": False}
    panel.set_flow({"id": "f", "nodes": [{"id": "n", "title": "节点", "tool_names": ["read_file", "calc"], "custom_tools": [definition]}], "edges": []})
    panel.nodes["n"].setSelected(True)
    app.processEvents()
    assert panel.custom_tool_checks["calc"].isChecked()
    panel.custom_tool_checks["calc"].setChecked(False)
    assert panel.nodes["n"].data["tool_names"] == ["read_file"]
    panel.custom_tool_checks["calc"].setChecked(True)
    panel.custom_tools_edit.setPlainText("[{")
    assert panel.nodes["n"].data["custom_tools"] == "[{"
    assert "无效" in panel.custom_tools_hint.text()
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    assert not panel._contract_valid()
    assert "动态工具定义无效" in warnings[0]
    panel.custom_tools_edit.setPlainText("[]")
    assert panel.nodes["n"].data["tool_names"] == ["read_file"]
    panel.close()
