"""共享设备台独立 HTTP 验收；仅启动指定可信产物，不代写业务代码。"""
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


def contract_view(path: str, value: dict, *, method: str = "GET") -> dict:
    """保留契约字段与记录数量，忽略时间、展示等额外字段。"""
    def integer(item, key, minimum=0, maximum=None):
        number = item.get(key)
        if type(number) is not int or number < minimum or (maximum is not None and number > maximum):
            raise ValueError("invalid_integer")

    def text(item, key):
        content = item.get(key)
        if not isinstance(content, str) or content != content.strip() or not 1 <= len(content) <= 80:
            raise ValueError("invalid_text")

    def record(item, kind):
        if not isinstance(item, dict):
            raise ValueError("invalid_record")
        integer(item, "id", 1)
        if kind == "equipment":
            text(item, "name")
            integer(item, "total", 1, 10000)
            integer(item, "available", 0, item["total"])
            keys = ("id", "name", "total", "available")
        else:
            integer(item, "equipment_id", 1)
            integer(item, "quantity", 1, 10000)
            text(item, "borrower")
            if item.get("status") not in ("active", "returned"):
                raise ValueError("invalid_status")
            keys = ("id", "equipment_id", "borrower", "quantity", "status")
        return {key: item[key] for key in keys}

    route = path.split("?", 1)[0]
    if route == "/api/stats":
        keys = ("equipment_types", "total_units", "available_units", "active_loans")
        for key in keys:
            integer(value, key)
        return {key: value[key] for key in keys}
    if route == "/api/equipment":
        equipment = value.get("equipment")
        if method == "GET":
            if not isinstance(equipment, list):
                raise ValueError("invalid_list")
            return {"equipment": [record(row, "equipment") for row in equipment]}
        return {"equipment": record(equipment, "equipment")}
    if route == "/api/loans" and method == "GET":
        if not isinstance(value.get("loans"), list):
            raise ValueError("invalid_list")
        return {"loans": [record(row, "loan") for row in value["loans"]]}
    if route.startswith("/api/loans"):
        return {"loan": record(value.get("loan"), "loan")}
    return value


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
        if 200 <= expected < 300:
            try:
                value = contract_view(path, value, method=method)
            except ValueError:
                check(name + "_schema", False)
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
             "--port", str(port), "--db-file", str(data)], cwd=project,
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
        with tempfile.TemporaryDirectory(prefix="equipment-validation-") as temp:
            data = Path(temp) / "equipment.sqlite"
            try:
                launch(data)
                run_contract(check, api, request, stop, lambda: launch(data), data)
            finally:
                # 数据目录仅在自有服务及其后代退出后销毁。
                stop()
            result["passed"] = True
    except ValidationFailure as error:
        result["failure"] = str(error)
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        result["failure"] = "transport_or_artifact_error"
    finally:
        stop()
    return result


def check_sqlite(check, data: Path) -> None:
    """只读验证真实 SQLite；不依赖业务表名，不创建或修复数据库。"""
    import sqlite3

    check("sqlite_file", data.is_file() and data.read_bytes()[:16] == b"SQLite format 3\x00")
    try:
        connection = sqlite3.connect(data.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        rows = []
    check("sqlite_integrity", rows == [("ok",)])


def run_contract(check, api, request, stop, restart, data):
    """对外部服务验证库存事务、输入边界与重启持久化。"""
    from concurrent.futures import ThreadPoolExecutor

    check("initial_equipment_empty", api("initial", "GET", "/api/equipment", 200) == {"equipment": []})
    status, html, mime = request("GET", "/")
    parser = _FormParser()
    parser.feed(html.decode("utf-8"))
    check("html_form", status == 200 and "text/html" in mime
          and {"form", "input", "button"} <= parser.tags
          and b"/static/style.css" in html and b"/static/app.js" in html)
    for asset, mimes in (("style.css", ("text/css",)),
                         ("app.js", ("application/javascript", "text/javascript"))):
        status, body, mime = request("GET", "/static/" + asset)
        check("asset_" + asset, status == 200 and bool(body.strip())
              and mime.split(";", 1)[0].strip() in mimes)
    equipment = api("create", "POST", "/api/equipment", 201,
                    {"name": "  团队投影仪  ", "total": 5}).get("equipment")
    check("created_equipment_schema", isinstance(equipment, dict))
    eid = equipment.get("id")
    check("created_equipment", type(eid) is int and eid > 0
          and equipment.get("name") == "团队投影仪"
          and type(equipment.get("total")) is int and equipment["total"] == 5
          and type(equipment.get("available")) is int and equipment["available"] == 5)

    def snapshot():
        equipment_state = api("snapshot_equipment", "GET", "/api/equipment", 200)
        active = api("snapshot_active", "GET", "/api/loans?status=active", 200)
        returned = api("snapshot_returned", "GET", "/api/loans?status=returned", 200)
        all_loans = api("snapshot_all_loans", "GET", "/api/loans", 200).get("loans")
        active_rows, returned_rows = active.get("loans"), returned.get("loans")
        check("all_loans_schema", all(isinstance(rows, list) for rows in (all_loans, active_rows, returned_rows)))
        # 按完整 JSON 规范化排序，保留重复条目，避免依赖业务返回顺序。
        canonical = lambda rows: sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
        check("all_loans_union", canonical(all_loans) == canonical(active_rows + returned_rows))
        return (equipment_state, active, returned,
                api("snapshot_stats", "GET", "/api/stats", 200))

    def error(name, method, path, expected, payload=None, raw=None):
        before = snapshot()
        value = api(name, method, path, expected, payload, raw)
        check(name + "_error", isinstance(value.get("error"), str) and bool(value["error"].strip()))
        check(name + "_unchanged", snapshot() == before)

    check("initial_loans", api("loans", "GET", "/api/loans?status=active", 200) == {"loans": []})
    check("initial_stats", api("stats", "GET", "/api/stats", 200)
          == {"equipment_types": 1, "total_units": 5, "available_units": 5, "active_loans": 0})
    invalid = []
    for value in (None, True, 0, -1, 10001, 1.5, "5"):
        invalid.append(("/api/equipment", {"name": "设备", "total": value}))
        invalid.append(("/api/loans", {"equipment_id": eid, "borrower": "张三", "quantity": value}))
    for value in (None, 1, "", "   ", "x" * 81):
        invalid.append(("/api/equipment", {"name": value, "total": 5}))
        invalid.append(("/api/loans", {"equipment_id": eid, "borrower": value, "quantity": 1}))
    for value in (None, True, 0, -1, "1", 1.5):
        invalid.append(("/api/loans", {"equipment_id": value, "borrower": "张三", "quantity": 1}))
    for path in ("/api/equipment", "/api/loans"):
        for value in ([], None, True, {}, "bad"):
            invalid.append((path, value))
    for i, (path, payload) in enumerate(invalid):
        # JSON null 需要显式原始正文，不能解释成无正文。
        error("invalid_" + str(i), "POST", path, 400, raw=json.dumps(payload))
    for i, path in enumerate(("/api/equipment", "/api/loans")):
        error("bad_json_" + str(i), "POST", path, 400, raw="{")
    error("bad_filter", "GET", "/api/loans?status=invalid", 400)
    error("unknown_api", "GET", "/api/unknown", 404)
    error("unknown_equipment", "POST", "/api/loans", 404,
          {"equipment_id": eid + 100000, "borrower": "张三", "quantity": 1})
    error("unknown_loan", "POST", "/api/loans/99999999/return", 404, {})
    error("insufficient", "POST", "/api/loans", 409,
          {"equipment_id": eid, "borrower": "张三", "quantity": 6})

    barrier = threading.Barrier(2)

    def borrow(name):
        barrier.wait(timeout=5)
        return request("POST", "/api/loans", {"equipment_id": eid, "borrower": "  " + name + "  ", "quantity": 3})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(borrow, name) for name in ("张三", "李四")]
        responses = [future.result() for future in futures]
    check("concurrent_inventory_status", sorted(r[0] for r in responses) == [201, 409])
    successful = next(r for r in responses if r[0] == 201)
    rejected = next(r for r in responses if r[0] == 409)
    check("concurrent_mime", all("application/json" in r[2] for r in responses))
    rejected_body = json.loads(rejected[1])
    check("concurrent_error", isinstance(rejected_body, dict)
          and isinstance(rejected_body.get("error"), str) and bool(rejected_body["error"].strip()))
    body = json.loads(successful[1])
    check("loan_response_object", isinstance(body, dict))
    try:
        body = contract_view("/api/loans", body, method="POST")
    except ValueError:
        check("loan_schema", False)
    loan = body.get("loan")
    check("loan_schema", isinstance(loan, dict))
    lid = loan.get("id")
    check("loan_fields", type(lid) is int and lid > 0 and loan.get("equipment_id") == eid
          and type(loan.get("equipment_id")) is int
          and loan.get("borrower") in ("张三", "李四")
          and type(loan.get("quantity")) is int and loan["quantity"] == 3 and loan.get("status") == "active")
    borrowed = snapshot()
    check("concurrent_inventory", borrowed[0] == {"equipment": [{**equipment, "available": 2}]}
          and borrowed[1] == {"loans": [loan]} and borrowed[2] == {"loans": []}
          and borrowed[3] == {"equipment_types": 1, "total_units": 5, "available_units": 2, "active_loans": 1})
    for i, raw in enumerate(("{", "[]", "null", "true", '"invalid"', "1")):
        error("invalid_return_" + str(i), "POST", f"/api/loans/{lid}/return", 400, raw=raw)
    check_sqlite(check, data)
    stop()
    restart()
    check("restart_active", snapshot() == borrowed)
    returned = api("return", "POST", f"/api/loans/{lid}/return", 200, {}).get("loan")
    check("return_schema", isinstance(returned, dict))
    check("return_fields", all(returned.get(key) == value for key, value in
          {"id": lid, "equipment_id": eid, "borrower": loan["borrower"], "quantity": 3, "status": "returned"}.items()))
    settled = snapshot()
    check("return_inventory", settled[0] == {"equipment": [equipment]}
          and settled[1] == {"loans": []} and settled[2] == {"loans": [returned]}
          and settled[3] == {"equipment_types": 1, "total_units": 5, "available_units": 5, "active_loans": 0})
    error("double_return", "POST", f"/api/loans/{lid}/return", 409, {})
    stop()
    restart()
    check("restart_returned", snapshot() == settled)
    # 接受合法边界，防止把全部边界输入拒绝也算作正确校验。
    for count in (1, 10000):
        name = "设" * (1 if count == 1 else 80)
        borrower = "人" * (1 if count == 1 else 80)
        edge = api(f"boundary_equipment_{count}", "POST", "/api/equipment", 201,
                   {"name": name, "total": count}).get("equipment")
        check(f"boundary_equipment_schema_{count}", isinstance(edge, dict))
        check(f"boundary_equipment_fields_{count}", type(edge.get("id")) is int
              and edge["id"] > 0 and edge["id"] != eid and edge.get("name") == name
              and type(edge.get("total")) is int and edge["total"] == count
              and type(edge.get("available")) is int and edge["available"] == count)
        edge_loan = api(f"boundary_borrow_{count}", "POST", "/api/loans", 201,
                        {"equipment_id": edge["id"], "borrower": borrower, "quantity": count}).get("loan")
        check(f"boundary_loan_schema_{count}", isinstance(edge_loan, dict))
        check(f"boundary_loan_fields_{count}", type(edge_loan.get("id")) is int
              and edge_loan["id"] > 0 and edge_loan["id"] != lid
              and edge_loan.get("equipment_id") == edge["id"]
              and edge_loan.get("borrower") == borrower
              and type(edge_loan.get("quantity")) is int and edge_loan["quantity"] == count
              and edge_loan.get("status") == "active")
        rows = api(f"boundary_inventory_{count}", "GET", "/api/equipment", 200).get("equipment")
        check(f"boundary_inventory_zero_{count}", isinstance(rows, list)
              and {**edge, "available": 0} in rows)
        value = api(f"boundary_return_{count}", "POST", f"/api/loans/{edge_loan['id']}/return", 200, {}).get("loan")
        check(f"boundary_returned_{count}", isinstance(value, dict) and value.get("status") == "returned")
    for i, path in enumerate(("/static/../app.py", "/static/%2e%2e/app.py",
                              "/static/../equipment.sqlite", "/static/%2e%2e/equipment.sqlite",
                              "/app.py", "/equipment.sqlite")):
        status, _, _ = request("GET", path)
        check("path_boundary_" + str(i), status in (400, 403, 404))


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
