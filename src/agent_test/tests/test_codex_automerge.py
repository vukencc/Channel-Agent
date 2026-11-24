import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location("codex_automerge", Path(__file__).resolve().parents[3] / ".github/scripts/codex_automerge.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def summary(sha="head"):
    clean = {"commit_sha": sha, "status": "completed", "has_any_findings": False, "has_unresolved_findings": False}
    empty = dict.fromkeys(clean)
    return {"id": 1, "updated_at": "2026-01-01", "user": {"id": 199175422, "type": "Bot", "login": "chatgpt-codex-connector[bot]"},
            "performed_via_github_app": {"id": 1144995},
            "body": gate.MARKER + "\n" + gate.PREFIX + json.dumps({"code_review": clean, "security_review": empty}) + " -->"}


def test_summary_requires_identity_current_head_and_no_findings():
    comment = summary()
    assert gate.approved([comment], "head")
    receipt = {**comment, "id": 2, "body": "<!-- codex-review-receipt:v1 -->\nReviewed commit"}
    assert gate.approved([comment, receipt], "head")
    assert not gate.approved([receipt], "head")
    assert not gate.approved([comment], "new")
    forged = summary()
    forged["performed_via_github_app"]["id"] = 1
    assert not gate.approved([forged], "head")
    blocked = summary()
    blocked["body"] = blocked["body"].replace('"has_any_findings": false', '"has_any_findings": true', 1)
    assert not gate.approved([blocked], "head")
    assert not gate.approved([comment, {**summary("new"), "id": 2}], "head")
    security = summary()
    security["body"] = security["body"].replace('"status": null', '"status": "in_progress"')
    assert not gate.approved([security], "head")


def test_merge_state_and_ci_require_exact_association():
    pr = {"number": 7, "state": "open", "draft": False, "mergeable": True, "mergeable_state": "clean", "labels": [],
          "head": {"sha": "head", "ref": "branch", "repo": {"id": 5}}, "base": {"sha": "base", "ref": "main"}}
    run = {"id": 3, "workflow_id": 4, "event": "pull_request", "head_sha": "head", "head_branch": "branch", "head_repository": {"id": 5},
           "pull_requests": [{"number": 7, "base": {"sha": "base"}, "head": {"sha": "head"}}], "status": "completed", "conclusion": "success"}
    jobs = lambda *_: [{"name": n, "status": "completed", "conclusion": "success"} for n in ("offline", "windows")]
    assert gate.eligible(pr) and gate.ci_ok([run], jobs, pr, 4)
    assert not gate.ci_ok([{**run, "pull_requests": []}], jobs, pr, 4)
    assert not gate.ci_ok([{**run, "head_repository": {"id": 6}}], jobs, pr, 4)
    assert not gate.ci_ok([run], lambda *_: jobs()[:1], pr, 4)
    assert not gate.ci_ok([run, {**run, "run_number": 2, "status": "in_progress", "conclusion": None}], jobs, pr, 4)
    assert not gate.eligible({**pr, "mergeable_state": "unknown"})
    assert not gate.eligible({**pr, "mergeable_state": "protected"})
    assert not gate.eligible({**pr, "base": {"ref": "other"}})
    assert not gate.eligible({**pr, "labels": [{"name": "hold"}]})


def test_check_requires_protected_main_and_safe_changed_files():
    class FakeAPI:
        protected = True
        default = "main"
        files = [{"filename": "docs/readme.md"}]

        def request(self, path, data=None):
            if path == "":
                return {"default_branch": self.default}
            if path == "/branches/main":
                return {"protected": self.protected}
            if path == "/pulls/7":
                return {"number": 7, "state": "open", "draft": False, "mergeable": True,
                        "mergeable_state": "clean", "labels": [], "changed_files": 1,
                        "head": {"sha": "head", "ref": "branch", "repo": {"id": 5}},
                        "base": {"sha": "base", "ref": "main"}}
            if path.startswith("/contents/"):
                return {"sha": "same"}
            raise AssertionError(path)

        def pages(self, path, key=None):
            if path.endswith("/files"):
                return self.files
            if path.endswith("/comments"):
                return [summary()]
            if "/runs?" in path:
                return [{"id": 3, "workflow_id": 4, "event": "pull_request", "head_sha": "head", "head_branch": "branch", "head_repository": {"id": 5},
                         "pull_requests": [{"number": 7, "base": {"sha": "base"}, "head": {"sha": "head"}}], "status": "completed", "conclusion": "success"}]
            return [{"name": n, "status": "completed", "conclusion": "success"} for n in ("offline", "windows")]

    api = FakeAPI()
    assert gate.check(api, 7, 4) == "head"
    api.protected = False
    assert gate.check(api, 7, 4) is None
    api.protected = True
    api.default = "other"
    assert gate.check(api, 7, 4) is None
    api.default = "main"
    for filename in (".github/workflows/tests.yml", ".github/actions/setup/action.yml", ".github/scripts/gate.py"):
        api.files = [{"filename": filename}]
        assert gate.check(api, 7, 4) is None
    api.files = [{"filename": "safe", "previous_filename": ".github/scripts/gate.py"}]
    assert gate.check(api, 7, 4) is None
    api.files = []
    assert gate.check(api, 7, 4) is None


def test_main_rechecks_before_merge_and_requires_confirmation(monkeypatch):
    class FakeAPI:
        merged = False

        def request(self, path, data=None):
            if path == "/actions/workflows/tests.yml":
                return {"id": 4}
            self.merged = True
            return {"merged": False}

        def pages(self, path):
            return [{"number": 7}]

    api = FakeAPI()
    monkeypatch.setattr(gate, "API", lambda: api)
    checks = iter(["head", None])
    monkeypatch.setattr(gate, "check", lambda *_: next(checks))
    assert gate.main() == 0
    assert not api.merged
    monkeypatch.setattr(gate, "check", lambda *_: "head")
    assert gate.main() == 1
    assert api.merged
