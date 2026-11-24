"""根据官方 Codex 审查和回归结果，保守判断是否合并。"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

MARKER = "<!-- codex-pull-request-review-summary -->"
PREFIX = "<!-- codex-review-state:v1 "


def approved(comments: list[dict[str, Any]], sha: str) -> bool:
    trusted = [c for c in comments if c.get("user", {}).get("id") == 199175422
               and c.get("user", {}).get("type") == "Bot"
               and c.get("user", {}).get("login") == "chatgpt-codex-connector[bot]"
               and (c.get("performed_via_github_app") or {}).get("id") == 1144995
               and not (c.get("body") or "").startswith("<!-- codex-review-receipt:v1 -->")]
    if not trusted:
        return False
    comment = max(trusted, key=lambda c: (c.get("updated_at", ""), c.get("id", 0)))
    lines = (comment.get("body") or "").splitlines()
    if len(lines) < 2 or lines[0] != MARKER or not lines[1].startswith(PREFIX) or not lines[1].endswith(" -->"):
        return False
    try:
        state = json.loads(lines[1][len(PREFIX):-4])
        code, security = state["code_review"], state["security_review"]
        def good(review: dict[str, Any]) -> bool:
            return (review.get("status") == "completed" and review.get("commit_sha") == sha
                    and review.get("has_any_findings") is False and review.get("has_unresolved_findings") is False)
        keys = ("status", "commit_sha", "has_any_findings", "has_unresolved_findings")
        absent = all(k in security and security[k] is None for k in keys)
        return good(code) and (absent or good(security))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def eligible(pr: dict[str, Any]) -> bool:
    return (pr.get("state") == "open" and pr.get("draft") is False
            and pr.get("base", {}).get("ref") == "main"
            and pr.get("mergeable") is True and pr.get("mergeable_state") == "clean"
            and not any(label.get("name", "").lower() == "hold" for label in pr.get("labels", [])))


def ci_ok(runs: list[dict[str, Any]], jobs_for: Callable[..., list[dict[str, Any]]], pr: dict[str, Any], workflow_id: int) -> bool:
    head = pr["head"]
    candidates = [r for r in runs if r.get("workflow_id") == workflow_id
                  and r.get("event") == "pull_request" and r.get("head_sha") == head["sha"]
                  and (r.get("head_repository") or {}).get("id") == head["repo"]["id"]
                  and r.get("head_branch") == head["ref"]
                  and any(p.get("number") == pr["number"] and p.get("base", {}).get("sha") == pr["base"]["sha"]
                          and p.get("head", {}).get("sha") == head["sha"] for p in r.get("pull_requests", []))]
    if not candidates:
        return False
    run = max(candidates, key=lambda r: (r.get("run_number", 0), r.get("run_attempt", 0)))
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return False
    jobs = jobs_for(run["id"], run.get("run_attempt", 1))
    return all(sum(j.get("name") == name and j.get("status") == "completed" and j.get("conclusion") == "success" for j in jobs) == 1 for name in ("offline", "windows"))


class API:
    def __init__(self) -> None:
        self.base = "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"]
        self.token = os.environ["GH_TOKEN"]

    def request(self, path: str, data: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                                     headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                                     method="GET" if data is None else "PUT")
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def pages(self, path: str, key: str | None = None) -> list[dict[str, Any]]:
        results = []
        for page in range(1, 21):
            result = self.request(path + ("&" if "?" in path else "?") + "per_page=100&page=" + str(page))
            items = result if key is None else result[key]
            results.extend(items)
            if len(items) < 100:
                return results
        raise ValueError("pagination limit reached")


def check(api: API, number: int, workflow: int) -> str | None:
    if api.request("").get("default_branch") != "main" or api.request("/branches/main").get("protected") is not True:
        return None
    pr = api.request(f"/pulls/{number}")
    if not eligible(pr) or not pr.get("head", {}).get("repo"):
        return None
    sha = pr["head"]["sha"]
    files = api.pages(f"/pulls/{number}/files")
    if len(files) != pr.get("changed_files") or len(files) >= 3000:
        return None
    protected = (".github/workflows/", ".github/actions/", ".github/scripts/")
    if any(not isinstance(f.get("filename"), str)
           or (f.get("status") == "renamed" and not isinstance(f.get("previous_filename"), str)) or any(
            isinstance(f.get(key), str) and f[key].startswith(protected)
            for key in ("filename", "previous_filename")) for f in files):
        return None
    if not approved(api.pages(f"/issues/{number}/comments"), sha):
        return None
    # 再核对工作流文件，避免依赖变更文件列表的单一判断。
    path = "/contents/.github/workflows/tests.yml?ref="
    base = api.request(path + urllib.parse.quote(pr["base"]["sha"], safe=""))
    head = api.request(path + urllib.parse.quote(sha, safe=""))
    if not base.get("sha") or base.get("sha") != head.get("sha"):
        return None
    runs = api.pages(f"/actions/workflows/{workflow}/runs?event=pull_request&head_sha={sha}", "workflow_runs")
    jobs = lambda run, attempt: api.pages(f"/actions/runs/{run}/attempts/{attempt}/jobs", "jobs")
    return sha if ci_ok(runs, jobs, pr, workflow) else None


def main() -> int:
    api = API()
    workflow = api.request("/actions/workflows/tests.yml")["id"]
    failed = False
    for item in api.pages("/pulls?state=open"):
        number = item["number"]
        try:
            sha = check(api, number, workflow)
            if sha and check(api, number, workflow) == sha:
                try:
                    result = api.request(f"/pulls/{number}/merge", {"sha": sha})
                except urllib.error.HTTPError as exc:
                    if exc.code in (405, 409):
                        print(f"PR #{number}: merge state changed ({exc.code})")
                        continue
                    raise
                if result.get("merged") is not True:
                    raise ValueError("merge response did not confirm success")
                print(f"PR #{number}: merged {sha}")
            else:
                print(f"PR #{number}: gate not satisfied")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"::error::PR #{number}: gate unavailable ({type(exc).__name__})", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"::error::自动合并检查失败 ({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
