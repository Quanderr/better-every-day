from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cached_property
from urllib.parse import quote

from bed.connectors.http import HttpApi
from bed.core.models import BOT_LABEL, FileChange
from bed.core.ports import BotPr, PrRef, ReadToken, VcsError

API_SUFFIX = "/api/v4"
READ_TOKEN_DAYS = 2
REPORTER = 20
DEVELOPER = 30
STATES = {"opened": "open", "merged": "merged", "closed": "closed", "locked": "open"}


def _enc(value: str) -> str:
    return quote(value, safe="")


class GitLabConnector:
    def __init__(self, token: str, api_url: str = "https://gitlab.com/api/v4", static_read_token: str | None = None):
        self.http = HttpApi(api_url, {"PRIVATE-TOKEN": token}, VcsError)
        base = api_url.rstrip("/")
        self.web_url = base.removesuffix(API_SUFFIX)
        self.static_read_token = static_read_token
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._levels: dict[tuple[str, int], int] = {}
        self._exists_cache: dict[tuple[str, str, str], bool] = {}

    def _project(self, repo: str) -> str:
        return f"/projects/{_enc(repo)}"

    @cached_property
    def _own_user_id(self) -> int:
        return self.http.send("GET", "/user").json()["id"]

    def clone_url(self, repo: str) -> str:
        return f"{self.web_url}/{repo}.git"

    def read_token(self, repo: str) -> ReadToken:
        if self.static_read_token:
            return ReadToken(token=self.static_read_token)
        expires = self.now().astimezone(UTC).date() + timedelta(days=READ_TOKEN_DAYS)
        data = self.http.send("POST", f"{self._project(repo)}/access_tokens", json={
            "name": f"{BOT_LABEL}-read-{expires.isoformat()}",
            "scopes": ["read_repository"],
            "access_level": REPORTER,
            "expires_at": expires.isoformat(),
        }).json()
        return ReadToken(token=data["token"], revoke_id=str(data["id"]))

    def revoke_read_token(self, repo: str, revoke_id: str) -> None:
        self.http.send("DELETE", f"{self._project(repo)}/access_tokens/{_enc(revoke_id)}", ok_status=(404,))

    def read_file(self, repo: str, ref: str, path: str) -> str | None:
        response = self.http.send("GET", f"{self._project(repo)}/repository/files/{_enc(path)}/raw",
                                  params={"ref": ref}, ok_status=(404,))
        self._exists_cache[(repo, ref, path)] = response.status_code != 404
        return None if response.status_code == 404 else response.text

    def _bot_pr(self, mr: dict) -> BotPr:
        return BotPr(number=mr["iid"], url=mr["web_url"], state=STATES.get(mr["state"], "closed"),
                     branch=mr.get("source_branch", ""))

    def list_bot_prs(self, repo: str) -> list[BotPr]:
        out = []
        for mr in self.http.paginate(f"{self._project(repo)}/merge_requests", {"labels": BOT_LABEL, "state": "all"}):
            pr = self._bot_pr(mr)
            if pr.state == "open":
                diffs = self.http.paginate(f"{self._project(repo)}/merge_requests/{pr.number}/diffs", {})
                files = sorted({p for d in diffs for p in (d.get("new_path"), d.get("old_path")) if p})
                pr = pr.model_copy(update={"files": files})
            out.append(pr)
        return out

    def get_pr(self, repo: str, number: int) -> BotPr:
        return self._bot_pr(self.http.send("GET", f"{self._project(repo)}/merge_requests/{number}").json())

    def close_comment(self, repo: str, number: int) -> str:
        notes = self.http.paginate(f"{self._project(repo)}/merge_requests/{number}/notes", {"sort": "asc"})
        for note in reversed(notes):
            author = note.get("author", {}).get("id")
            if note.get("system") or author is None or author == self._own_user_id:
                continue
            if self._access_level(repo, author) >= DEVELOPER:
                return note.get("body", "")
        return ""

    def _access_level(self, repo: str, user_id: int) -> int:
        key = (repo, user_id)
        if key not in self._levels:
            response = self.http.send("GET", f"{self._project(repo)}/members/all/{user_id}", ok_status=(404,))
            self._levels[key] = 0 if response.status_code == 404 else int(response.json().get("access_level", 0))
        return self._levels[key]

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self.http.send("PUT", f"{self._project(repo)}/merge_requests/{number}", json={"add_labels": ",".join(labels)})

    def is_ancestor(self, repo: str, sha: str, branch: str) -> bool:
        response = self.http.send("GET", f"{self._project(repo)}/repository/merge_base",
                                  params=[("refs[]", sha), ("refs[]", branch)], ok_status=(400, 404))
        if response.status_code in (400, 404):
            return False
        return response.json().get("id") == sha

    def find_branch(self, repo: str, branch: str) -> str | None:
        response = self.http.send("GET", f"{self._project(repo)}/repository/branches/{_enc(branch)}",
                                  ok_status=(404,))
        return None if response.status_code == 404 else response.json()["commit"]["id"]

    def _exists(self, repo: str, ref: str, path: str) -> bool:
        cached = self._exists_cache.get((repo, ref, path))
        if cached is not None:
            return cached
        response = self.http.send("HEAD", f"{self._project(repo)}/repository/files/{_enc(path)}",
                                  params={"ref": ref}, ok_status=(404,))
        return response.status_code != 404

    def push_branch(self, repo: str, branch: str, base_sha: str, message: str, changes: list[FileChange]) -> str:
        actions = []
        for change in changes:
            if change.content is None:
                actions.append({"action": "delete", "file_path": change.path})
            else:
                action = "update" if self._exists(repo, base_sha, change.path) else "create"
                actions.append({"action": action, "file_path": change.path, "content": change.content})
        commit = self.http.send("POST", f"{self._project(repo)}/repository/commits", json={
            "branch": branch, "start_sha": base_sha, "commit_message": message, "actions": actions,
        }).json()
        return commit["id"]

    def find_pr(self, repo: str, branch: str) -> PrRef | None:
        mrs = self.http.send("GET", f"{self._project(repo)}/merge_requests",
                             params={"source_branch": branch, "state": "all"}).json()
        return PrRef(number=mrs[0]["iid"], url=mrs[0]["web_url"]) if mrs else None

    def open_pr(self, repo: str, base_branch: str, branch: str, title: str, body: str, labels: list[str]) -> PrRef:
        mr = self.http.send("POST", f"{self._project(repo)}/merge_requests", json={
            "source_branch": branch, "target_branch": base_branch, "title": title, "description": body,
            "labels": ",".join(labels), "remove_source_branch": True,
        }).json()
        return PrRef(number=mr["iid"], url=mr["web_url"])


def create(secret: dict[str, str], options: dict[str, str]) -> GitLabConnector:
    return GitLabConnector(
        token=secret["token"],
        api_url=options.get("api_url", "https://gitlab.com/api/v4"),
        static_read_token=secret.get("read_token"),
    )
