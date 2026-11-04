"""可复用的五 Agent 设备借用业务用例；创建定义，不隐式启动模型。"""
from __future__ import annotations

import json
from pathlib import Path

from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService


def load_case() -> dict:
    """契约和节点职责随 AgentTest 一起分发，不包含项目路径或凭据。"""
    path = Path(__file__).with_name("cases") / "equipment_lending_flow.json"
    return json.loads(path.read_text(encoding="utf-8"))


def create_case(flows: FlowService, projects: ProjectService,
                workspace: Path, *, title: str | None = None) -> dict[str, str]:
    """在调用方管理器中导入模板和项目，保留现有目录内容，运行需显式调用。"""
    selected, _ = projects.manager.workspaces.validate_path(str(workspace))
    case = load_case()
    label = title or case["title"]
    template = flows.create(title=label, handoff=case["handoff"],
                            execution_environment=case["execution_environment"],
                            permission_policy=case["permission_policy"])
    template.update(nodes=case["nodes"], edges=case["edges"])
    template = flows.save(template)
    project = projects.create(
        title=label, workspace_path=selected, flow_id=template["id"],
        handoff="在本项目实现团队共享设备借用台；五个节点按各自职责依次工作。",
    )
    return {"template": template["id"], "project": project["id"],
            "flow": project["flow_id"]}


def main() -> None:
    """仅导出可审阅的用例定义，不修改应用状态或调用 provider。"""
    print(json.dumps(load_case(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
