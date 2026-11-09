"""最终状态持久化失败时不得自动重放节点。"""
import asyncio
import errno
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from agent_core import config
from agent_core.core import flows as flows_module
from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.mark.parametrize("failure", ["before_publish", "before_publish_restart", "post_replace", "directory_sync", "notification"])
def test_completion_write_failure_preserves_queue_and_retry(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def model(history, **kwargs):
            calls.append(1)
            started.set()
            await release.wait()
            return {"role": "assistant", "content": "完成"}
        project = tmp_path / "project"
        project.mkdir()
        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        projects = ProjectService(manager, service)
        identifier = projects.create("项目", str(project))["flow_id"]
        flow = service.get(identifier)
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "工作", "reuse_session": True}]
        service.save(flow)
        real_write = store.atomic_write
        real_sync = flows_module.fsync_directory
        real_notify = service.on_change
        failed = False
        notified = []
        def fail_final(path, data):
            nonlocal failed
            if failure.startswith("before_publish") and Path(path) == service._path(identifier) and (json.loads(data).get("run") or {}).get("status") == "completed":
                raise OSError(errno.ENOSPC, "disk full")
            if failure == "post_replace" and not failed and Path(path) == service._path(identifier) and (json.loads(data).get("run") or {}).get("status") == "completed":
                real_write(path, data)
                failed = True
                raise OSError(errno.EIO, "ambiguous publication")
            return real_write(path, data)
        def fail_sync(path):
            nonlocal failed
            if failure == "directory_sync" and not failed and json.loads(service._path(identifier).read_text())["run"]["status"] == "completed":
                failed = True
                raise OSError(errno.EIO, "sync failed")
            return real_sync(path)
        def fail_notify():
            nonlocal failed
            notified.append((service.records[identifier].get("run") or {}).get("status"))
            if failure == "notification" and not failed and json.loads(service._path(identifier).read_text())["run"]["status"] == "completed":
                failed = True
                raise RuntimeError("notification failed")
            return real_notify()
        monkeypatch.setattr(store, "atomic_write", fail_final)
        monkeypatch.setattr(flows_module, "fsync_directory", fail_sync)
        monkeypatch.setattr(service, "on_change", fail_notify)
        try:
            service.start(identifier, input_text="first")
            await asyncio.wait_for(started.wait(), 5)
            service.start(identifier, input_text="queued")
            release.set()
            await asyncio.wait_for(asyncio.shield(service.tasks[identifier]), 5)
            if failure == "post_replace":
                assert service.records[identifier]["run"]["status"] == "persistence_failed"
                disk = json.loads(service._path(identifier).read_text())
                assert disk["run"]["status"] == "completed"
                assert len(disk["pending_runs"]) == 1
                with pytest.raises(PermissionError):
                    service.get(identifier)
                with pytest.raises(PermissionError):
                    service.retry_completion(identifier)
                assert json.loads(service._path(identifier).read_text()) == disk
                assert len(calls) == 1
                return
            current = service.get(identifier)
            if failure == "notification":
                assert current["run"]["status"] == "completed"
                assert "completed" in notified
                assert len(calls) == 2
                return
            assert current["run"]["status"] == "persistence_failed"
            assert len(current["pending_runs"]) == 1
            assert len(calls) == 1
            assert not manager.project_reservations
            disk = json.loads(service._path(identifier).read_text())
            assert disk["run"]["status"] == ("running" if failure.startswith("before_publish") else "completed")
            with pytest.raises(ValueError):
                service.start(identifier)
            monkeypatch.setattr(store, "atomic_write", real_write)
            monkeypatch.setattr(flows_module, "fsync_directory", real_sync)
            monkeypatch.setattr(service, "on_change", real_notify)
            if failure != "before_publish_restart":
                service.retry_completion(identifier)
                assert service.get(identifier)["run"]["status"] == "completed"
                assert len(service.get(identifier)["pending_runs"]) == 1
                assert len(calls) == 1
        finally:
            release.set()
            await service.shutdown()
            await manager.shutdown()
            store.close()
        if failure == "before_publish_restart":
            store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
            manager = SessionManager(store, model=model)
            service = FlowService(manager)
            ProjectService(manager, service)
            try:
                restored = service.get(identifier)
                assert restored["run"]["status"] == "interrupted"
                assert len(restored["pending_runs"]) == 1
                assert len(calls) == 1
            finally:
                await manager.shutdown()
                store.close()
    asyncio.run(run())


def test_web_retry_entry_does_not_replay(tmp_path, monkeypatch, sandbox_env):
    from fastapi.testclient import TestClient
    from agent_frontend.web.app import create_app

    monkeypatch.setattr(config, "RAG_ASSESS", False)
    calls = []
    async def model(history, **kwargs):
        calls.append(1)
        return {"role": "assistant", "content": "完成"}
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    app = create_app(store=store, token="flow-test-token", model=model)
    project = tmp_path / "project"
    project.mkdir()
    try:
        with TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer flow-test-token"}) as client:
            created = client.post("/api/projects", json={"title": "项目", "workspace_path": str(project)})
            assert created.status_code == 201, created.text
            identifier = created.json()["flow_id"]
            flow = client.get(f"/api/flows/{identifier}").json()
            flow.pop("project")
            flow["nodes"] = [{"id": "a", "title": "A", "prompt": "工作"}]
            assert client.put(f"/api/flows/{identifier}", json=flow).status_code == 200
            real_write = store.atomic_write
            def fail_final(path, data):
                if Path(path) == app.state.flows._path(identifier) and (json.loads(data).get("run") or {}).get("status") == "completed":
                    raise OSError(errno.ENOSPC, "private storage details")
                return real_write(path, data)
            monkeypatch.setattr(store, "atomic_write", fail_final)
            assert client.post(f"/api/flows/{identifier}/run", json={"input_text": "首轮"}).status_code == 200
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = client.get(f"/api/flows/{identifier}").json()
                if (state.get("run") or {}).get("status") == "persistence_failed":
                    break
                time.sleep(.01)
            assert state["run"]["status"] == "persistence_failed"
            assert "private storage details" not in state["run"]["error"]
            failed_retry = client.post(f"/api/flows/{identifier}/retry-completion")
            assert failed_retry.status_code == 409
            assert "private storage details" not in failed_retry.text
            monkeypatch.setattr(store, "atomic_write", real_write)
            result = client.post(f"/api/flows/{identifier}/retry-completion")
            assert result.status_code == 200, result.text
            assert result.json()["run"]["status"] == "completed"
            assert len(calls) == 1
            assert client.post(f"/api/flows/{identifier}/retry-completion").status_code == 409
    finally:
        store.close()


def test_desktop_retry_button_preserves_input():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    from agent_frontend.desktop.flow import FlowPanel

    app = QApplication.instance() or QApplication([])
    panel = FlowPanel()
    commands = []
    panel.command.connect(lambda action, arguments: commands.append((action, arguments)))
    panel.set_flow({"id": "flow", "title": "项目", "project_id": "project", "project": {"id": "project", "title": "项目"},
                    "nodes": [], "edges": [], "revision": 1, "run": {"status": "persistence_failed"}})
    panel.run_input.setPlainText("保留的下一轮输入")
    assert panel.run_button.text() == "重试保存"
    assert "节点不会重跑" in panel.status.text()
    panel.run_button.click()
    assert commands == [("flow_retry_completion", {"flow_id": "flow"})]
    assert panel.run_input.toPlainText() == "保留的下一轮输入"
    panel.deleteLater()
    app.processEvents()


def test_web_controller_retry_preserves_draft_and_input(tmp_path):
    node = shutil.which("node")
    assert node, "需要 Linux Node"
    asset = Path(__file__).resolve().parents[2] / "agent_frontend/web/static/flow.js"
    script = tmp_path / "retry.cjs"
    script.write_text(r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
let source = fs.readFileSync(process.argv[2], 'utf8');
source = source.replace('function render() {', 'function render() { return;');
source = source.replace('window.FlowUI = {', `window.harness={
  setup(b,f){bridge=b;active=f.id;drafts.set(f.id,f);dirty.add(f.id);runInputs.set(f.id,'下一轮')},
  perform, draft:()=>drafts.get(active), input:()=>runInputs.get(active), dirty:()=>dirty.has(active)
}; window.FlowUI = {`);
global.window = {}; global.document = {getElementById:()=>null};
vm.runInThisContext(source);
const f={id:'a'.repeat(32), project_id:'p', title:'未保存草稿', nodes:[],edges:[],revision:1,run:{status:'persistence_failed'}};
const calls=[];
window.harness.setup({api(path,options){
  if(options){calls.push([path,options]);return Promise.resolve({...f,title:'服务端',run:{status:'completed'}})}
  if(path==='/api/flows')return Promise.resolve({flows:[],flows_revision:0});
  return Promise.resolve({...f,run:{status:'completed'}});
},toast(message){throw Error(message)}},f);
(async()=>{
  await window.harness.perform('retry-completion');
  assert.equal(calls.length,1); assert.ok(calls[0][0].endsWith('/retry-completion'));
  assert.deepEqual(calls[0][1].body,{}); assert.equal(window.harness.input(),'下一轮');
  assert.equal(window.harness.draft().title,'未保存草稿'); assert.equal(window.harness.dirty(),true);
  assert.equal(window.harness.draft().run.status,'completed');
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding="utf-8")
    result = subprocess.run([node, str(script), str(asset)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
