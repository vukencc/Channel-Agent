"""独立验收器回归：受控坏产物，不调用模型。"""
import threading
import json
import os
import signal
import time
from pathlib import Path

import pytest

from agent_test.scripts.validate_flow_fullstack import validate_project


@pytest.mark.parametrize("bad_task", [None, []])
def test_invalid_created_task_schema_is_structured_failure(tmp_path, bad_task):
    (tmp_path / "app.py").write_text('''import argparse, json
from http.server import HTTPServer, BaseHTTPRequestHandler
p=argparse.ArgumentParser()
p.add_argument('--host'); p.add_argument('--port', type=int); p.add_argument('--data-file')
a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/tasks':
            body=b'{"tasks":[]}'; mime='application/json'
        elif self.path == '/':
            body=b'<form><input><button></button></form><link href="/static/style.css"><script src="/static/app.js"></script>'; mime='text/html'
        else:
            body=b'fixture'; mime='text/plain'
        self.send_response(200); self.send_header('Content-Type',mime); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        self.send_response(201); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps({'task': BAD_TASK}).encode())
HTTPServer((a.host,a.port),Handler).serve_forever()
'''.replace('BAD_TASK', repr(bad_task)))
    result = validate_project(tmp_path, timeout=2)
    assert result["failure"] == "contract_failed"
    assert result["checks"][-1] == {"name": "created_task_schema", "passed": False}


def test_missing_content_type_is_structured_failure(tmp_path):
    (tmp_path / "app.py").write_text('''import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
p=argparse.ArgumentParser()
p.add_argument('--host'); p.add_argument('--port', type=int); p.add_argument('--data-file')
a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"tasks":[]}')
HTTPServer((a.host,a.port),Handler).serve_forever()
''')
    result = validate_project(tmp_path, timeout=2)
    assert result["failure"] == "contract_failed"
    assert result["checks"][-1] == {"name": "initial_http", "passed": False}


@pytest.mark.parametrize("cancelled", [False, True])
def test_slow_http_body_is_bounded_and_cancellable(tmp_path, cancelled):
    (tmp_path / "app.py").write_text('''import argparse, time
from http.server import HTTPServer, BaseHTTPRequestHandler
p=argparse.ArgumentParser()
p.add_argument('--host'); p.add_argument('--port', type=int); p.add_argument('--data-file')
a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', '1000000')
        self.end_headers()
        for _ in range(1000000):
            self.wfile.write(b' '); self.wfile.flush(); time.sleep(0.03)
HTTPServer((a.host,a.port),Handler).serve_forever()
''')
    event = threading.Event()
    timer = threading.Timer(0.2, event.set)
    # 失败实现也必须有界退出，定时终止自有服务，避免回归挂起。
    import subprocess
    original = subprocess.Popen
    processes = []
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    from unittest.mock import patch
    def emergency_stop():
        for process in processes:
            if process.poll() is None:
                process.terminate()
    emergency = threading.Timer(1.5, emergency_stop)
    start = time.monotonic()
    emergency.start()
    if cancelled:
        timer.start()
    try:
        with patch.object(subprocess, 'Popen', launch):
            result = validate_project(tmp_path, timeout=0.35, cancel=event)
        assert result["failure"] == ("cancelled" if cancelled else "request_timeout")
        assert time.monotonic() - start < 1.2
    finally:
        timer.cancel()
        emergency.cancel()
        emergency_stop()
        for process in processes:
            process.wait(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组回归")
@pytest.mark.parametrize("leader_exits", [True, False])
def test_cleanup_kills_term_ignoring_descendant(tmp_path, leader_exits):
    (tmp_path / "app.py").write_text(
        "import os, signal, time\nfrom pathlib import Path\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    Path('child.pid').write_text(str(os.getpid()))\n"
        "    while True: time.sleep(0.05)\n"
        "while not Path('child.pid').exists(): time.sleep(0.01)\n"
        + ("raise SystemExit(0)\n" if leader_exits else "while True: time.sleep(0.05)\n")
    )
    child_pid = None
    try:
        result = validate_project(tmp_path, timeout=0.8)
        child_pid = int((tmp_path / "child.pid").read_text())
        assert result["failure"] == ("server_exited" if leader_exits else "startup_timeout")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            state = Path(f"/proc/{child_pid}/stat")
            if not state.exists() or state.read_text().split(") ", 1)[1].startswith("Z"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("验收器返回后仍有运行中的后代进程")
    finally:
        # 即使被测清理失败，也只回收本测试生成并记录的进程。
        pid_file = tmp_path / "child.pid"
        if child_pid is None and pid_file.exists():
            child_pid = int(pid_file.read_text())
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_service_receives_only_minimal_environment(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "AWS_SECRET_ACCESS_KEY",
                 "HTTPS_PROXY", "PYTHONPATH", "HOME", "UNRELATED_SETTING"):
        monkeypatch.setenv(name, "controlled-private-value")
    monkeypatch.setenv("LANG", "C.UTF-8")
    (tmp_path / "app.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "Path('child_environment.json').write_text(json.dumps({'names': list(os.environ), "
        "'path': os.environ.get('PATH'), 'lang': os.environ.get('LANG')}))\n"
    )
    result = validate_project(tmp_path, timeout=2)
    assert result["failure"] == "server_exited"
    environment = json.loads((tmp_path / "child_environment.json").read_text())
    allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
    if os.name == "nt":
        allowed.update({"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"})
    assert set(environment["names"]) <= allowed
    assert environment["path"] == os.environ["PATH"]
    assert environment["lang"] == "C.UTF-8"


def test_missing_entry_rejected(tmp_path):
    result = validate_project(tmp_path, timeout=0.2)
    assert result["passed"] is False
    assert result["failure"] == "missing_entry"


def test_bad_server_rejected(tmp_path):
    (tmp_path / "app.py").write_text("raise SystemExit(0)\n")
    result = validate_project(tmp_path, timeout=0.3)
    assert result["passed"] is False
    assert result["failure"] == "server_exited"


def test_timeout_rejected(tmp_path):
    (tmp_path / "app.py").write_text("import time\ntime.sleep(30)\n")
    result = validate_project(tmp_path, timeout=0.2)
    assert result["passed"] is False
    assert result["failure"] == "startup_timeout"


def test_cancelled_before_launch(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("raise RuntimeError('must not launch')\n")
    event = threading.Event()
    event.set()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("launched"))
    result = validate_project(tmp_path, cancel=event)
    assert result["failure"] == "cancelled"


def test_launch_permission_failure_is_reported(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("pass\n")
    def denied(*args, **kwargs):
        raise PermissionError("private path must not leak")
    monkeypatch.setattr("subprocess.Popen", denied)
    result = validate_project(tmp_path)
    assert result["failure"] == "transport_or_artifact_error"
    assert "private path" not in str(result)


def test_cancel_during_startup_stops_process(tmp_path, monkeypatch):
    import subprocess
    launched = []
    original = subprocess.Popen
    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", record)
    (tmp_path / "app.py").write_text("import time\ntime.sleep(30)\n")
    event = threading.Event()
    timer = threading.Timer(0.15, event.set)
    timer.start()
    try:
        result = validate_project(tmp_path, timeout=2, cancel=event)
    finally:
        timer.cancel()
    assert result["failure"] == "cancelled"
    assert len(launched) == 1
    assert launched[0].poll() is not None


def test_wrong_http_response_rejected(tmp_path):
    (tmp_path / "app.py").write_text('''import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
p=argparse.ArgumentParser()
p.add_argument('--host'); p.add_argument('--port', type=int); p.add_argument('--data-file')
a=p.parse_args()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b'{}')
HTTPServer((a.host,a.port),Handler).serve_forever()
''')
    result = validate_project(tmp_path, timeout=2)
    assert result["passed"] is False
    assert result["failure"] == "contract_failed"
    assert result["checks"][-1] == {"name": "initial_empty", "passed": False}
