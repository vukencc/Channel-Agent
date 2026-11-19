import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from agent_frontend.desktop.flow import FlowPanel


def test_resume_button_keeps_draft_and_prevents_duplicate():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    flow = {"id": "f", "title": "Flow", "revision": 1, "project_id": "p", "project": {"id": "p"}, "nodes": [], "edges": [], "run": {"id": "r", "status": "checkpoint", "resume_available": True}}
    panel.set_flow(flow)
    commands = []
    panel.command.connect(lambda action, payload: commands.append((action, payload)))
    assert panel.run_button.text() == "从中断节点继续"
    panel.run_button.click()
    panel.run_button.click()
    assert commands == [("flow_resume", {"flow_id": "f", "run_id": "r"})]
    panel.accept_result("flow_resume", {"error": "stale"})
    panel.add_node()
    assert not panel.run_button.isEnabled()
    assert panel.is_dirty
    panel.close()


def test_desktop_bridge_forwards_resume_and_returns_post_audit_snapshot(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from agent_frontend.desktop.runtime import RuntimeBridge

    QApplication.instance() or QApplication([])
    bridge = RuntimeBridge(tmp_path / "state")
    calls = []
    bridge._manager = SimpleNamespace(sessions={})
    bridge._flows = SimpleNamespace(
        resume=lambda flow_id, *, run_id: calls.append((flow_id, run_id)) or {"id": flow_id},
        get=lambda flow_id: {"id": flow_id, "run": {"id": "r", "status": "running"}},
    )
    bridge._audit = lambda action, **kwargs: calls.append((action, kwargs))
    result = asyncio.run(bridge._dispatch("flow_resume", {"flow_id": "f", "run_id": "r"}))
    assert calls == [("f", "r"), ("flow_resume", {"flow_id": "f"})]
    assert result["run"]["status"] == "running"


def test_late_resume_ack_does_not_roll_back_completed_state():
    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    base = {"id": "f", "title": "Flow", "revision": 1, "project_id": "p", "project": {"id": "p"}, "nodes": [], "edges": []}
    panel.set_flow({**base, "run": {"id": "r", "resume_available": True, "status": "checkpoint"}})
    panel.run_button.click()
    panel.set_flow({**base, "run": {"id": "r", "resume_available": False, "status": "completed"}})
    panel.accept_result("flow_resume", {**base, "run": {"id": "r", "resume_available": False, "status": "running"}})
    assert panel._flow["run"]["status"] == "completed"
    panel.close()
