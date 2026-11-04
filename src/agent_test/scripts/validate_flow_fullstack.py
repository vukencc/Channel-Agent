"""独立 HTTP 产物验收；仅运行显式指定的可信项目，不代表浏览器交互验证。"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from html.parser import HTMLParser


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)


class ValidationFailure(Exception):
    pass


def validate_project(project: Path, *, timeout: float = 8,
                     cancel: threading.Event | None = None) -> dict:
    """使用独立临时数据文件；不读取项目已有业务数据，不输出服务正文。"""
    project = Path(project).resolve()
    checks = []
    result = {"evidence": "independent_http_execution", "passed": False,
              "checks": checks, "failure": None}
    process = None
    port = 0

    def stopped():
        if cancel is not None and cancel.is_set():
            raise ValidationFailure("cancelled")

    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        if not condition:
            raise ValidationFailure("contract_failed")

    def stop():
        nonlocal process
        if process is not None:
            if os.name == "posix":
                # 此进程组由 start_new_session 独占；主进程退出不代表后代结束。
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    # 即使主进程已退出，也要杀死忽略 TERM 的组内后代。
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=2)
            elif process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            process = None

    def request(method, path, payload=None, raw=None):
        stopped()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        body = raw if raw is not None else json.dumps(payload) if payload is not None else None
        deadline = time.monotonic() + timeout
        finished = threading.Event()
        aborted = []
        watcher = None

        def boundary():
            stopped()
            if aborted:
                raise ValidationFailure(aborted[0])
            if time.monotonic() >= deadline:
                raise ValidationFailure("request_timeout")

        def guard(connection_socket):
            while not finished.wait(0.02):
                reason = ("cancelled" if cancel is not None and cancel.is_set()
                          else "request_timeout" if time.monotonic() >= deadline else None)
                if reason:
                    aborted.append(reason)
                    # shutdown 可中断正在读响应头或正文的调用，close 单独使用不可靠。
                    try:
                        connection_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return

        try:
            conn.connect()
            boundary()
            watcher = threading.Thread(target=guard, args=(conn.sock,), daemon=True)
            watcher.start()
            conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            chunks = []
            size = 0
            while True:
                boundary()
                chunk = response.read1(min(65536, 1_000_001 - size))
                boundary()
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 1_000_000:
                    raise ValidationFailure("response_too_large")
            return response.status, b"".join(chunks), response.getheader("Content-Type", "")
        except (OSError, http.client.HTTPException):
            boundary()
            raise
        finally:
            finished.set()
            conn.close()
            if watcher is not None:
                watcher.join(timeout=1)

    def api(name, method, path, expected, payload=None, raw=None):
        status, body, mime = request(method, path, payload, raw)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            value = None
        check(name + "_http", status == expected and "application/json" in mime
              and isinstance(value, dict))
        return value

    def launch(data):
        nonlocal process, port
        stopped()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # 业务服务只继承运行必需环境，不传递模型、云服务凭据或代理配置。
        allowed_environment = {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
        if os.name == "nt":
            allowed_environment.update({"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"})
        environment = {name: os.environ[name] for name in allowed_environment
                       if name in os.environ}
        process = subprocess.Popen(
            [sys.executable, str(project / "app.py"), "--host", "127.0.0.1",
             "--port", str(port), "--data-file", str(data)], cwd=project,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix", env=environment)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stopped()
            if process.poll() is not None:
                raise ValidationFailure("server_exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.03)
        raise ValidationFailure("startup_timeout")

    try:
        stopped()
        if not (project / "app.py").is_file():
            raise ValidationFailure("missing_entry")
        with tempfile.TemporaryDirectory(prefix="flow-http-validation-") as temp:
            data = Path(temp) / "tasks.json"
            launch(data)
            check("initial_empty", api("initial", "GET", "/api/tasks", 200) == {"tasks": []})
            status, html, mime = request("GET", "/")
            parser = _FormParser()
            parser.feed(html.decode("utf-8"))
            check("html_form", status == 200 and "text/html" in mime
                  and {"form", "input", "button"} <= parser.tags
                  and b"/static/style.css" in html and b"/static/app.js" in html)
            for asset in ("style.css", "app.js"):
                status, body, _ = request("GET", "/static/" + asset)
                check("asset_" + asset, status == 200 and len(body.strip()) > 0)
            created = api("create", "POST", "/api/tasks", 201, {"title": "  验收任务  "}).get("task", {})
            check("created_task_schema", isinstance(created, dict))
            identifier = created.get("id")
            check("created_task", isinstance(identifier, str) and bool(identifier)
                  and created.get("title") == "验收任务" and created.get("done") is False)
            from urllib.parse import quote
            task_path = "/api/tasks/" + quote(identifier, safe="")
            updated = api("update", "PATCH", task_path, 200, {"title": "已更新", "done": True})
            check("updated_task", updated.get("task") == {"id": identifier, "title": "已更新", "done": True})
            check("stats", api("stats", "GET", "/api/stats", 200)
                  == {"total": 1, "completed": 1, "pending": 0})
            for state, expected in (("pending", []), ("completed", [updated["task"]])):
                check("filter_" + state, api("filter_" + state, "GET", "/api/tasks?status=" + state, 200)
                      == {"tasks": expected})
            invalid = [("POST", "/api/tasks", {"title": " "}, None),
                       ("POST", "/api/tasks", {"title": "x" * 121}, None),
                       ("POST", "/api/tasks", {"title": 2}, None),
                       ("POST", "/api/tasks", None, "{"),
                       ("PATCH", task_path, {}, None),
                       ("PATCH", task_path, {"done": "false"}, None),
                       ("GET", "/api/tasks?status=invalid", None, None)]
            for i, (method, path, payload, raw) in enumerate(invalid):
                error = api("invalid_" + str(i), method, path, 400, payload, raw)
                check("invalid_error_" + str(i), isinstance(error.get("error"), str) and bool(error["error"]))
            for i, (method, path) in enumerate((("GET", "/api/unknown"),
                                              ("PATCH", "/api/tasks/no-such-id"),
                                              ("DELETE", "/api/tasks/no-such-id"))):
                error = api("missing_" + str(i), method, path, 404, {"done": True} if method == "PATCH" else None)
                check("missing_error_" + str(i), isinstance(error.get("error"), str) and bool(error["error"]))
            for i, path in enumerate(("/static/../app.py", "/static/%2e%2e/app.py")):
                status, _, _ = request("GET", path)
                check("path_boundary_" + str(i), status in (400, 403, 404))
            check("persistence_file", data.is_file())
            stop()
            launch(data)
            check("restart_update", api("restart", "GET", "/api/tasks", 200) == {"tasks": [updated["task"]]})
            check("delete", api("delete", "DELETE", task_path, 200) == {"deleted": True})
            stop()
            launch(data)
            check("restart_delete", api("restart_deleted", "GET", "/api/tasks", 200) == {"tasks": []})
            newer = api("new_id", "POST", "/api/tasks", 201, {"title": "新任务"}).get("task", {})
            check("new_task_schema", isinstance(newer, dict))
            check("id_not_reused", isinstance(newer.get("id"), str) and bool(newer["id"]) and newer["id"] != identifier)
            result["passed"] = True
    except ValidationFailure as error:
        result["failure"] = str(error)
    except (OSError, ValueError, http.client.HTTPException):
        result["failure"] = "transport_or_artifact_error"
    finally:
        stop()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_project(args.project, timeout=args.timeout)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
