"""持久化项目身份及其独立流程副本。"""
import copy
import json
import os
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.core.files import opened_path, fsync_directory
from agent_core.core.flows import Definition, _order


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=1, max_length=120)
    workspace_path: str | None = None
    workspace_identity: tuple[int, int] | None = None
    flow_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_flow_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    execution_environment: Literal["sandbox", "local"] | None = None
    permission_policy: Literal["full_access", "smart", "readonly"] | None = None
    model_profile: str | None = None
    handoff: str = ""
    revision: int = Field(ge=0, default=0)


class ProjectService:
    def __init__(self, manager, flows, on_change=None):
        self.manager = manager
        self.flows = flows
        self.on_change = on_change or manager.notify
        self.directory = manager.store.root / "projects"
        if self.directory.is_symlink():
            raise PermissionError("项目目录不能是符号链接")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.records = {}
        self.file_identities = {}
        self.load_errors = []
        for path in self.directory.glob("*.json"):
            if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                self.load_errors.append("部分项目文件无效，已隔离")
                continue
            try:
                value = self._read(path.stem)
                self.records[path.stem] = value
            except (ValueError, OSError, TypeError):
                self.load_errors.append("部分项目文件无效，已隔离")
                continue
        flows.projects = self
        manager.workspaces.projects = self

    @property
    def errors(self):
        errors = list(self.load_errors) + list(self.flows.load_errors)
        for identifier, value in self.records.items():
            try:
                self._check_link(identifier, value["flow_id"])
            except (KeyError, ValueError, OSError):
                errors.append("部分项目与 Flow 的关联无效，已隔离")
        for value in self.flows.records.values():
            if value["project_id"]:
                try:
                    self._check_link(value["project_id"], value["id"])
                except (KeyError, ValueError, OSError):
                    errors.append("部分项目与 Flow 的关联无效，已隔离")
        return errors

    def _check_link(self, project_id, flow_id):
        project = self.records[project_id]
        stored = self._read(project_id)
        flow = self.flows.records[flow_id]
        if (stored["flow_id"] != flow_id or project["flow_id"] != flow_id
                or flow["project_id"] != project_id or not self.flows._path(flow_id).exists()):
            raise ValueError("项目与 Flow 的关联无效")
        self.flows._verify_link_file(flow_id)

    def _path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("无效项目 ID")
        path = self.directory / (identifier + ".json")
        if self.directory.is_symlink() or path.is_symlink():
            raise PermissionError("项目文件不能是符号链接")
        return path

    def _read(self, identifier):
        path = self._path(identifier)
        with opened_path(self.directory, path) as fd:
            info = os.fstat(fd)
            with os.fdopen(os.dup(fd), encoding="utf-8") as stream:
                value = Project.model_validate(json.load(stream)).model_dump()
        if value["id"] != identifier:
            raise PermissionError("项目文件身份不匹配")
        identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        previous = self.file_identities.get(identifier)
        if previous is not None and (identity != previous or (identifier in self.records and value != self.records[identifier])):
            raise PermissionError("项目文件已在外部变化")
        if previous is None:
            self.file_identities[identifier] = identity
        return value

    def _write(self, value):
        path = self._path(value["id"])
        if not path.exists() and value["id"] in self.records:
            raise PermissionError("项目文件已在外部删除")
        if path.exists():
            self._read(value["id"])
        self.manager.store.atomic_write(path, json.dumps(value, ensure_ascii=False, allow_nan=False))
        info = path.stat(follow_symlinks=False)
        self.file_identities[value["id"]] = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        fsync_directory(self.directory)
        self.on_change()

    def list(self):
        values = []
        for identifier in self.records:
            try:
                values.append(self.get(identifier))
            except (KeyError, ValueError, OSError):
                continue
        return values

    def get(self, project_id):
        self.records[project_id]  # 只允许已加载的项目身份
        value = self._read(project_id)
        if value["flow_id"] != self.records[project_id]["flow_id"]:
            raise PermissionError("项目流程身份已变化")
        self._check_link(project_id, value["flow_id"])
        return copy.deepcopy(value)

    def _workspace(self, value):
        if not value["workspace_path"]:
            if value["workspace_identity"] is not None:
                raise PermissionError("项目工作区身份无效")
            return None
        path, identity = self.manager.workspaces.validate_path(value["workspace_path"])
        if path != value["workspace_path"] or value["workspace_identity"] is None or tuple(value["workspace_identity"]) != tuple(identity):
            raise PermissionError("项目工作区身份已变化")
        return tuple(identity)

    def _check_idle(self, *identities):
        identities = {identity for identity in identities if identity is not None}
        if identities:
            self.flows._check_idle(identities, None)

    def create(self, title, workspace_path, flow_id=None, execution_environment=None,
               permission_policy=None, model_profile=None, handoff=""):
        identifier, instance_id = uuid.uuid4().hex, uuid.uuid4().hex
        path, identity = self.manager.workspaces.validate_path(workspace_path)
        value = Project(id=identifier, title=title, workspace_path=path, workspace_identity=identity, flow_id=instance_id,
                        source_flow_id=flow_id, execution_environment=execution_environment,
                        permission_policy=permission_policy, model_profile=model_profile, handoff=handoff).model_dump()
        if flow_id:
            instance = self.flows.get(flow_id)
            if instance["project_id"]:
                raise ValueError("只能导入 Flow 模板")
        else:
            instance = Definition(id=instance_id, title=title, revision=0).model_dump()
        instance.update(id=instance_id, project_id=identifier, workspace_path=None, run=None, revision=0,
                        session_bindings={}, pending_runs=[])
        for node in instance["nodes"]:
            node["workspace_path"] = None
        instance = Definition.model_validate(instance).model_dump()
        _order(instance)
        for profile in [model_profile, instance["model_profile"], *(node["model_profile"] for node in instance["nodes"])]:
            if profile and not self.manager.model_registry.contains(profile):
                raise ValueError("未知模型 profile")
        if self._path(identifier).exists() or self.flows._path(instance_id).exists():
            raise FileExistsError("项目或 Flow 文件已存在")
        try:
            self.flows._write(instance)
        except BaseException:
            if instance_id in self.flows.file_identities:
                self.flows.records[instance_id] = instance
                try:
                    self.flows.delete(instance_id, project_id=identifier)
                except Exception:
                    pass
            raise
        self.flows.records[instance_id] = instance
        try:
            self._write(value)
        except BaseException:
            path = self._path(identifier)
            if path.exists() and self._read(identifier) == value:
                path.unlink()
            self.file_identities.pop(identifier, None)
            self.flows.delete(instance_id, project_id=identifier)
            raise
        self.records[identifier] = value
        return copy.deepcopy(value)

    def save(self, project):
        value = Project.model_validate(project).model_dump()
        old = self.get(value["id"])
        if value["flow_id"] != old["flow_id"] or value["source_flow_id"] != old["source_flow_id"]:
            raise ValueError("不能更改项目流程身份")
        if value["revision"] != old["revision"]:
            raise ValueError("项目版本已变化")
        task = self.flows.tasks.get(value["flow_id"])
        if task and not task.done():
            raise ValueError("运行期间不能编辑项目")
        if value["workspace_identity"] != old["workspace_identity"]:
            raise ValueError("不能直接更改项目工作区身份")
        old_identity = self._workspace(old)
        path, identity = self.manager.workspaces.validate_path(value["workspace_path"])
        if path != old["workspace_path"]:
            self._check_idle(old_identity, identity)
        value["workspace_path"] = path
        value["workspace_identity"] = identity
        if value["model_profile"] and not self.manager.model_registry.contains(value["model_profile"]):
            raise ValueError("未知模型 profile")
        value["revision"] += 1
        self._write(value)
        self.records[value["id"]] = value
        return copy.deepcopy(value)

    def update(self, project_id, **changes):
        value = self.get(project_id)
        value.update(changes)
        return self.save(value)

    def delete(self, project_id):
        value = self.get(project_id)
        task = self.flows.tasks.get(value["flow_id"])
        if task and not task.done():
            raise ValueError("运行期间不能删除项目")
        self._check_idle(self._workspace(value))
        self._read(project_id)
        flow = self.flows.get(value["flow_id"])
        try:
            self.flows.delete(value["flow_id"], project_id=project_id)
            self._read(project_id)
            self._path(project_id).unlink()
        except BaseException:
            flow_path = self.flows._path(value["flow_id"])
            if not flow_path.exists():
                self.flows.records.pop(value["flow_id"], None)
                self.flows.file_identities.pop(value["flow_id"], None)
                self.flows._write(flow)
                self.flows.records[value["flow_id"]] = flow
            raise
        fsync_directory(self.directory)
        del self.records[project_id]
        self.file_identities.pop(project_id, None)
        self.on_change()
