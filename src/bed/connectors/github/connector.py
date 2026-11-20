import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from bed.connectors.http import HttpApi
from bed.core.models import BOT_LABEL, FileChange
from bed.core.ports import BotPr, PrRef, ReadToken, VcsError

DEFAULT_MODE = "100644"
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
DEFAULT_API_URL = "https://api.github.com"
ENTERPRISE_SERVER_API_SUFFIX = "/api/v3"


def web_url_for(api_url: str) -> str:
    base = api_url.rstrip("/")
    if base.endswith(ENTERPRISE_SERVER_API_SUFFIX):
        return base.removesuffix(ENTERPRISE_SERVER_API_SUFFIX)
    return base.replace("://api.", "://", 1)


class GitHubConnector:
    def __init__(self, app_id: str, private_key: str, installation_id: str, api_url: str = DEFAULT_API_URL,
                 client: httpx.Client | None = None):
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.http = HttpApi(api_url, {"X-GitHub-Api-Version": "2022-11-28"}, VcsError, client=client)
        self.web_url = web_url_for(api_url)
        self._token: tuple[str, datetime] | None = None

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key,
                          algorithm="RS256")

    def _installation_token(self, body: dict[str, Any] | None = None) -> tuple[str, datetime]:
        data = self._send("POST", f"/app/installations/{self.installation_id}/access_tokens", json=body,
                          auth=f"Bearer {self._app_jwt()}").json()
        return data["token"], datetime.fromisoformat(data["expires_at"])

    def _auth(self) -> str:
        if self._token is None or self._token[1] - TOKEN_REFRESH_MARGIN <= datetime.now(UTC):
            self._token = self._installation_token()
        return f"Bearer {self._token[0]}"

    def _send(self, method: str, path: str, *, auth: str | None = None, ok404: bool = False,
              accept: str = "application/vnd.github+json", **kwargs: Any) -> httpx.Response:
        headers = {"Accept": accept, "Authorization": auth or self._auth()}
        return self.http.send(method, path, ok_status=(404,) if ok404 else (), headers=headers, **kwargs)

    def _paginate(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        headers = {"Accept": "application/vnd.github+json", "Authorization": self._auth()}
        return self.http.paginate(path, params, headers=headers)

    def clone_url(self, repo: str) -> str:
        return f"{self.web_url}/{repo}.git"

    def read_token(self, repo: str) -> ReadToken:
        token, _ = self._installation_token({"repositories": [repo.split("/", 1)[1]],
                                             "permissions": {"contents": "read"}})
        return ReadToken(token=token)

    def revoke_read_token(self, repo: str, revoke_id: str) -> None:
        return None

    def read_file(self, repo: str, ref: str, path: str) -> str | None:
        response = self._send("GET", f"/repos/{repo}/contents/{quote(path)}", params={"ref": ref}, ok404=True,
                              accept="application/vnd.github.raw+json")
        return None if response.status_code == 404 else response.text

    def list_bot_prs(self, repo: str) -> list[BotPr]:
        out = []
        for item in self._paginate(f"/repos/{repo}/issues", {"labels": BOT_LABEL, "state": "all"}):
            if "pull_request" not in item:
                continue
            state = "merged" if item["pull_request"].get("merged_at") else item["state"]
            files: list[str] = []
            if state == "open":
                files = [f["filename"] for f in self._paginate(f"/repos/{repo}/pulls/{item['number']}/files", {})]
            out.append(BotPr(number=item["number"], url=item["html_url"], state=state, files=files))
        return out

    def get_pr(self, repo: str, number: int) -> BotPr:
        pr = self._send("GET", f"/repos/{repo}/pulls/{number}").json()
        state = "merged" if pr.get("merged_at") else pr["state"]
        return BotPr(number=pr["number"], url=pr["html_url"], state=state, branch=pr["head"]["ref"])

    def close_comment(self, repo: str, number: int) -> str:
        comments = self._paginate(f"/repos/{repo}/issues/{number}/comments", {})
        human = [c for c in comments if c.get("user", {}).get("type") != "Bot"
                 and c.get("author_association") in TRUSTED_ASSOCIATIONS]
        return human[-1]["body"] if human else ""

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._send("POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})

    def is_ancestor(self, repo: str, sha: str, branch: str) -> bool:
        response = self._send("GET", f"/repos/{repo}/compare/{sha}...{quote(branch)}", ok404=True)
        if response.status_code == 404:
            return False
        return response.json().get("status") in ("ahead", "identical")

    def find_branch(self, repo: str, branch: str) -> str | None:
        response = self._send("GET", f"/repos/{repo}/git/ref/heads/{quote(branch)}", ok404=True)
        return None if response.status_code == 404 else response.json()["object"]["sha"]

    def push_branch(self, repo: str, branch: str, base_sha: str, message: str, changes: list[FileChange]) -> str:
        base_tree = self._send("GET", f"/repos/{repo}/git/commits/{base_sha}").json()["tree"]["sha"]
        listing = self._send("GET", f"/repos/{repo}/git/trees/{base_tree}", params={"recursive": "1"}).json()
        modes = {e["path"]: e["mode"] for e in listing.get("tree", []) if e.get("type") == "blob"}
        if listing.get("truncated"):
            modes = self._walk_modes(repo, base_tree, [c.path for c in changes])
        entries = []
        for change in changes:
            sha = None
            if change.content is not None:
                sha = self._send("POST", f"/repos/{repo}/git/blobs",
                                 json={"content": change.content, "encoding": "utf-8"}).json()["sha"]
            mode = modes.get(change.path, DEFAULT_MODE)
            entries.append({"path": change.path, "mode": mode, "type": "blob", "sha": sha})
        tree = self._send("POST", f"/repos/{repo}/git/trees", json={"base_tree": base_tree, "tree": entries}).json()
        commit = self._send("POST", f"/repos/{repo}/git/commits",
                            json={"message": message, "tree": tree["sha"], "parents": [base_sha]}).json()
        self._send("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return commit["sha"]

    def _walk_modes(self, repo: str, root_tree: str, paths: list[str]) -> dict[str, str]:
        cache: dict[str, dict[str, dict]] = {}

        def entries(tree_sha: str) -> dict[str, dict]:
            if tree_sha not in cache:
                listing = self._send("GET", f"/repos/{repo}/git/trees/{tree_sha}").json()
                cache[tree_sha] = {e["path"]: e for e in listing.get("tree", [])}
            return cache[tree_sha]

        modes: dict[str, str] = {}
        for path in paths:
            *dirs, name = path.split("/")
            tree_sha: str | None = root_tree
            for part in dirs:
                entry = entries(tree_sha).get(part)
                tree_sha = entry["sha"] if entry and entry.get("type") == "tree" else None
                if tree_sha is None:
                    break
            if tree_sha is not None:
                entry = entries(tree_sha).get(name)
                if entry and entry.get("type") == "blob":
                    modes[path] = entry["mode"]
        return modes

    def find_pr(self, repo: str, branch: str) -> PrRef | None:
        owner = repo.split("/", 1)[0]
        prs = self._send("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all"}).json()
        return PrRef(number=prs[0]["number"], url=prs[0]["html_url"]) if prs else None

    def open_pr(self, repo: str, base_branch: str, branch: str, title: str, body: str, labels: list[str]) -> PrRef:
        pr = self._send("POST", f"/repos/{repo}/pulls",
                        json={"title": title, "head": branch, "base": base_branch, "body": body}).json()
        if labels:
            self.add_labels(repo, pr["number"], labels)
        return PrRef(number=pr["number"], url=pr["html_url"])


def create(secret: dict[str, str], options: dict[str, str]) -> GitHubConnector:
    return GitHubConnector(
        app_id=secret["app_id"],
        private_key=secret["private_key"],
        installation_id=secret["installation_id"],
        api_url=options.get("api_url", DEFAULT_API_URL),
    )
