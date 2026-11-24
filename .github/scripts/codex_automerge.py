"""根据官方 Codex 审查和回归结果，保守判断是否合并。"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

MARKER = "<!-- codex-pull-request-review-summary -->"
PREFIX = "<!-- codex-review-state:v1 "
RECEIPT = "<!-- codex-review-receipt:v1 -->"
SHA = re.compile(r"[0-9a-f]{40}")
REVIEWED_COMMIT = re.compile(r"\*\*Reviewed commit:\*\* `([0-9a-f]{10,40})`")
THREAD_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequest {
      id
      headRefOid
      reviewThreads(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved comments(first: 1) { nodes { id } } }
      }
    }
  }
}
"""


def known_author(user: object) -> bool:
    return (isinstance(user, dict) and type(user.get("id")) is int and user["id"] > 0
            and isinstance(user.get("login"), str) and bool(user["login"])
            and isinstance(user.get("type"), str) and bool(user["type"]))


def codex_bot(user: object) -> bool:
    return (isinstance(user, dict) and user.get("id") == 199175422 and user.get("type") == "Bot"
            and user.get("login") == "chatgpt-codex-connector[bot]")


def approved(comments: list[dict[str, Any]], sha: str,
             verified_history: Callable[[str], bool] | None = None) -> bool:
    try:
        trusted = [c for c in comments if codex_bot(c.get("user"))
                   and (c.get("performed_via_github_app") or {}).get("id") == 1144995]
        latest = lambda c: (c.get("updated_at", ""), c.get("id", 0))
        summaries = [c for c in trusted if not (c.get("body") or "").startswith(RECEIPT)]
        if not summaries:
            return False
        lines = (max(summaries, key=latest).get("body") or "").splitlines()
        if len(lines) < 2 or lines[0] != MARKER or not lines[1].startswith(PREFIX) or not lines[1].endswith(" -->"):
            return False
        state = json.loads(lines[1][len(PREFIX):-4])
        code, security = state["code_review"], state["security_review"]

        def completed(review: dict[str, Any]) -> bool:
            return (review.get("status") == "completed" and review.get("commit_sha") == sha
                    and review.get("has_unresolved_findings") is False)

        keys = ("status", "commit_sha", "has_any_findings", "has_unresolved_findings")
        absent = all(k in security and security[k] is None for k in keys)
        if not completed(code) or not (absent or (completed(security) and security.get("has_any_findings") is False)):
            return False
        if code.get("has_any_findings") is False:
            return True
        if code.get("has_any_findings") is not True or verified_history is None:
            return False
        receipts = [c for c in trusted if (c.get("body") or "").startswith(RECEIPT)]
        if not receipts:
            return False
        lines = [line.strip() for line in max(receipts, key=latest)["body"].splitlines() if line.strip()]
        refs = [line for line in lines if line.startswith("**Reviewed commit:**")]
        reviewed = REVIEWED_COMMIT.fullmatch(refs[0]) if len(refs) == 1 else None
        # 只认可已核实的官方无新发现回执文本和提交标记。
        return (len(lines) >= 3 and lines[0] == RECEIPT
                and lines[1].startswith("Codex Review: Didn't find any major issues.")
                and reviewed is not None and sha.startswith(reviewed[1])
                and verified_history(reviewed[1]))
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

    def _request(self, url: str, method: str, data: dict[str, Any] | None) -> Any:
        req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                     headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                              "Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def request(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self._request(self.base + path, "GET" if data is None else "PUT", data)

    def graphql(self, query: str, variables: dict[str, Any]) -> Any:
        return self._request("https://api.github.com/graphql", "POST", {"query": query, "variables": variables})

    def pages(self, path: str, key: str | None = None) -> list[dict[str, Any]]:
        results = []
        for page in range(1, 21):
            result = self.request(path + ("&" if "?" in path else "?") + "per_page=100&page=" + str(page))
            items = result if key is None else result[key]
            results.extend(items)
            if len(items) < 100:
                return results
        raise ValueError("pagination limit reached")


def resolved_thread_roots(api: API, pr: dict[str, Any]) -> set[str] | None:
    """完整分页取得 GitHub 已解决线程的根评论，核对查询仍属于同一头提交。"""
    ident = pr.get("node_id")
    if not isinstance(ident, str) or not ident:
        return None
    root_ids: set[str] = set()
    thread_ids: set[str] = set()
    cursors: set[str] = set()
    cursor = None
    count = None
    for _ in range(20):
        result = api.graphql(THREAD_QUERY, {"id": ident, "cursor": cursor})
        if "errors" in result:
            return None
        node = result["data"]["node"]
        if node["id"] != ident or node["headRefOid"] != pr["head"]["sha"]:
            return None
        connection = node["reviewThreads"]
        total = connection["totalCount"]
        if type(total) is not int or not 0 < total <= 2000 or (count is not None and total != count):
            return None
        count = total
        for thread in connection["nodes"]:
            thread_id = thread["id"]
            roots = thread["comments"]["nodes"]
            if (thread["isResolved"] is not True or not isinstance(thread_id, str)
                    or not thread_id or thread_id in thread_ids or len(roots) != 1):
                return None
            root = roots[0]["id"]
            if not isinstance(root, str) or not root or root in root_ids:
                return None
            root_ids.add(root)
            thread_ids.add(thread_id)
        page = connection["pageInfo"]
        if page["hasNextPage"] is False:
            return root_ids if len(root_ids) == count else None
        cursor = page["endCursor"]
        if (page["hasNextPage"] is not True or not isinstance(cursor, str) or not cursor
                or cursor in cursors or len(root_ids) >= count):
            return None
        cursors.add(cursor)
    return None


def verified_resolved_findings(api: API, pr: dict[str, Any], reviewed: str) -> bool:
    """仅在历史发现可与已解决线程逐条配对时使用已完成的无新发现回执。"""
    sha = pr["head"]["sha"]
    if not SHA.fullmatch(sha) or api.request(f"/commits/{reviewed}").get("sha") != sha:
        return False
    path = f"/pulls/{pr['number']}"
    reviews = {}
    for row in api.pages(f"{path}/reviews"):
        if not known_author(row.get("user")):
            return False
        if codex_bot(row["user"]):
            key, commit = row["id"], row.get("commit_id")
            if (type(key) is not int or key <= 0 or key in reviews or not isinstance(commit, str)
                    or not SHA.fullmatch(commit) or commit == sha):
                return False
            reviews[key] = commit
    roots = set()
    root_comment_ids = set()
    reply_to = set()
    matched_reviews = set()
    original_commits = set()
    for row in api.pages(f"{path}/comments"):
        author = row.get("user")
        if not known_author(author):
            return False
        original = row.get("original_commit_id")
        reply = row.get("in_reply_to_id")
        if reply is not None:
            if type(reply) is not int or (codex_bot(author) and original not in reviews.values()):
                return False
            reply_to.add(reply)
            continue
        root, comment_id = row["node_id"], row["id"]
        if (not isinstance(root, str) or not root or root in roots or type(comment_id) is not int
                or comment_id <= 0 or comment_id in root_comment_ids or not isinstance(original, str)
                or not SHA.fullmatch(original) or original == sha):
            return False
        if codex_bot(author):
            review_id = row["pull_request_review_id"]
            if type(review_id) is not int or reviews.get(review_id) != original:
                return False
            matched_reviews.add(review_id)
        roots.add(root)
        root_comment_ids.add(comment_id)
        original_commits.add(original)
    if (not matched_reviews or matched_reviews != set(reviews) or not reply_to <= root_comment_ids
            or resolved_thread_roots(api, pr) != roots):
        return False
    for commit in sorted(original_commits):
        relation = api.request(f"/compare/{commit}...{sha}?per_page=1")
        if (relation.get("status") != "ahead" or type(relation.get("behind_by")) is not int
                or relation["behind_by"] != 0
                or (relation.get("merge_base_commit") or {}).get("sha") != commit):
            return False
    return True


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
    if not approved(api.pages(f"/issues/{number}/comments"), sha,
                    lambda reviewed: verified_resolved_findings(api, pr, reviewed)):
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
