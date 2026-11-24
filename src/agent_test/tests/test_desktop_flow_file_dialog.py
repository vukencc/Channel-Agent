"""Flow 文件对话框位置与成功回执。"""
import json
from pathlib import Path

import pytest
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from agent_frontend.desktop.flow import FlowPanel


def test_flow_file_dialog_remembers_only_success(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("AI_AGENT_PROJECT_ROOT", str(tmp_path))
    panel = FlowPanel()
    panel.set_flow({"id": "draft", "revision": 1, "nodes": [], "edges": []}, [])
    seen = []
    selected = tmp_path / "selected"
    selected.mkdir()
    source = selected / "input.json"
    source.write_text(json.dumps({"format": "agent-flow"}), encoding="utf-8")
    answers = iter([str(source), "", str(selected / "output.json"), ""])
    def dialog(*args):
        seen.append(args[2])
        return next(answers), ""
    monkeypatch.setattr(QFileDialog, "getOpenFileName", dialog)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", dialog)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    panel._import_file()
    panel.accept_result("flow_import", {"error": "bad"})
    panel._import_file()
    assert Path(seen[0]) == tmp_path
    assert Path(seen[1]) == tmp_path
    panel._export_file()
    assert Path(seen[2]) == tmp_path / "flow-template.json"
    panel.accept_result("flow_export", {"format": "agent-flow"})
    panel._import_file()
    assert Path(seen[3]) == selected
    panel.deleteLater()


def test_flow_import_success_and_export_failure_preserve_directory(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("AI_AGENT_PROJECT_ROOT", str(tmp_path))
    panel = FlowPanel()
    panel.set_flow({"id": "draft", "revision": 1, "nodes": [], "edges": []}, [])
    selected = tmp_path / "chosen"
    selected.mkdir()
    source = selected / "input.json"
    source.write_text("{}", encoding="utf-8")
    seen = []
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(source), ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (seen.append(args[2]) or str(tmp_path / "failed.json"), ""))
    panel._import_file()
    panel.accept_result("flow_import", {"id": "imported"})
    panel._export_file()
    panel.accept_result("flow_export", {"error": "failed"})
    panel._export_file()
    assert [Path(item) for item in seen] == [selected / "flow-template.json"] * 2
    panel.deleteLater()
