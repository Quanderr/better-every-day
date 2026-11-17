import json

import httpx
import jwt
import pytest
import respx

from bed.connectors.github import create
from bed.core.models import FileChange
from bed.core.ports import VcsError

API = "https://api.github.com"
TOKEN_URL = f"{API}/app/installations/42/access_tokens"
R = f"{API}/repos/acme/web"


@pytest.fixture
def gh(rsa_pem):
    conn = create({"app_id": "7", "private_key": rsa_pem, "installation_id": "42"}, {})
    conn.http.sleep = lambda s: None
    return conn


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).respond(201, json={"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
        yield mock


def body(call):
    return json.loads(call.request.content)


def test_app_jwt_signed_rs256(gh, api):
    route = api.get(f"{R}/git/ref/heads/x").respond(404)
    gh.find_branch("acme/web", "x")
    auth = api.calls[0].request.headers["authorization"]
    claims = jwt.decode(auth.split()[1], options={"verify_signature": False})
    assert claims["iss"] == "7" and claims["exp"] - claims["iat"] <= 600
    assert route.calls[0].request.headers["authorization"] == "Bearer inst-token"


def test_installation_token_cached(gh, api):
    api.get(f"{R}/git/ref/heads/x").respond(404)
    gh.find_branch("acme/web", "x")
    gh.find_branch("acme/web", "x")
    assert api.routes[0].call_count == 1


def test_read_token_scoped_to_one_repo(gh):
    with respx.mock() as mock:
        route = mock.post(TOKEN_URL).respond(201, json={"token": "ro", "expires_at": "2099-01-01T00:00:00Z"})
        tok = gh.read_token("acme/web")
        assert tok.token == "ro" and tok.revoke_id is None
    assert body(route.calls[0]) == {"repositories": ["web"], "permissions": {"contents": "read"}}
    assert gh._token is None


def test_clone_url(gh):
    assert gh.clone_url("acme/web") == "https://github.com/acme/web.git"


def test_read_file(gh, api):
    route = api.get(f"{R}/contents/src/a.py", params={"ref": "abc"}).respond(200, text="a = 1\n")
    assert gh.read_file("acme/web", "abc", "src/a.py") == "a = 1\n"
    assert "raw" in route.calls[0].request.headers["accept"]


def test_read_file_missing(gh, api):
    api.get(f"{R}/contents/x.py").respond(404, json={"message": "Not Found"})
    assert gh.read_file("acme/web", "abc", "x.py") is None


def pr(n, state="open", merged_at=None, labels=("better-every-day",)):
    return {"number": n, "html_url": f"https://github.com/acme/web/pull/{n}", "state": state,
            "merged_at": merged_at, "head": {"ref": f"b{n}"}, "labels": [{"name": label} for label in labels]}


def issue(n, state="open", merged_at=None, is_pr=True):
    item = {"number": n, "html_url": f"https://github.com/acme/web/pull/{n}", "state": state}
    if is_pr:
        item["pull_request"] = {"merged_at": merged_at}
    return item


def test_list_bot_prs_uses_label_filtered_issues_with_pagination(gh, api):
    page2 = f"{R}/issues?labels=better-every-day&state=all&per_page=100&page=2"
    api.get(f"{R}/issues", params={"page": "2"}).respond(200, json=[issue(4, "closed")])
    first = api.get(f"{R}/issues").respond(200, json=[issue(1), issue(2, "closed", "2026-01-01"),
                                                      issue(3, is_pr=False)],
                                           headers={"link": f'<{page2}>; rel="next"'})
    api.get(f"{R}/pulls/1/files").respond(200, json=[{"filename": "src/a.py"}])
    prs = {p.number: p for p in gh.list_bot_prs("acme/web")}
    assert dict(first.calls[0].request.url.params) == {"labels": "better-every-day", "state": "all",
                                                       "per_page": "100"}
    assert set(prs) == {1, 2, 4}
    assert prs[1].state == "open" and prs[1].files == ["src/a.py"]
    assert prs[2].state == "merged" and prs[2].files == []
    assert prs[4].state == "closed"


def test_close_comment_last_human(gh, api):
    api.get(f"{R}/issues/4/comments").respond(200, json=[
        {"body": "human says no", "user": {"type": "User"}, "author_association": "OWNER"},
        {"body": "bot noise", "user": {"type": "Bot"}, "author_association": "NONE"},
    ])
    assert gh.close_comment("acme/web", 4) == "human says no"


def test_add_labels(gh, api):
    route = api.post(f"{R}/issues/6/labels").respond(200, json=[])
    gh.add_labels("acme/web", 6, ["better-every-day"])
    assert body(route.calls[0]) == {"labels": ["better-every-day"]}


def test_find_branch(gh, api):
    api.get(f"{R}/git/ref/heads/WEB-1-x").respond(200, json={"object": {"sha": "s1"}})
    assert gh.find_branch("acme/web", "WEB-1-x") == "s1"


def test_push_branch(gh, api):
    api.post(f"{R}/git/blobs").respond(201, json={"sha": "blob1"})
    api.get(f"{R}/git/commits/base").respond(200, json={"tree": {"sha": "tree0"}})
    api.get(f"{R}/git/trees/tree0", params={"recursive": "1"}).respond(200, json={"tree": [
        {"path": "bin/run.sh", "mode": "100755", "type": "blob"},
        {"path": "gone.py", "mode": "100644", "type": "blob"},
    ]})
    trees = api.post(f"{R}/git/trees").respond(201, json={"sha": "tree1"})
    commits = api.post(f"{R}/git/commits").respond(201, json={"sha": "c1"})
    refs = api.post(f"{R}/git/refs").respond(201, json={})
    sha = gh.push_branch("acme/web", "WEB-1-x", "base", "msg",
                         [FileChange(path="a.py", content="x\n"), FileChange(path="gone.py", content=None),
                          FileChange(path="bin/run.sh", content="echo\n")])
    assert sha == "c1"
    tree = body(trees.calls[0])
    assert tree["base_tree"] == "tree0"
    assert {"path": "gone.py", "mode": "100644", "type": "blob", "sha": None} in tree["tree"]
    assert {"path": "a.py", "mode": "100644", "type": "blob", "sha": "blob1"} in tree["tree"]
    assert {"path": "bin/run.sh", "mode": "100755", "type": "blob", "sha": "blob1"} in tree["tree"]
    assert body(commits.calls[0]) == {"message": "msg", "tree": "tree1", "parents": ["base"]}
    assert body(refs.calls[0]) == {"ref": "refs/heads/WEB-1-x", "sha": "c1"}


def test_find_pr(gh, api):
    api.get(f"{R}/pulls", params={"head": "acme:WEB-1-x", "state": "all"}).respond(200, json=[pr(5)])
    assert gh.find_pr("acme/web", "WEB-1-x").number == 5


def test_find_pr_none(gh, api):
    api.get(f"{R}/pulls").respond(200, json=[])
    assert gh.find_pr("acme/web", "x") is None


def test_open_pr_adds_labels(gh, api):
    pulls = api.post(f"{R}/pulls").respond(201, json=pr(6))
    labels = api.post(f"{R}/issues/6/labels").respond(200, json=[])
    ref = gh.open_pr("acme/web", "main", "WEB-1-x", "title", "body", ["better-every-day"])
    assert ref.number == 6 and ref.url.endswith("/pull/6")
    assert body(pulls.calls[0]) == {"title": "title", "head": "WEB-1-x", "base": "main", "body": "body"}
    assert body(labels.calls[0]) == {"labels": ["better-every-day"]}


def test_5xx_retried_then_succeeds(gh, api):
    api.get(f"{R}/git/ref/heads/x").mock(side_effect=[httpx.Response(502), httpx.Response(200,
                                                      json={"object": {"sha": "s"}})])
    assert gh.find_branch("acme/web", "x") == "s"


def test_422_raises(gh, api):
    api.post(f"{R}/pulls").respond(422, json={"message": "Validation Failed"})
    with pytest.raises(VcsError) as err:
        gh.open_pr("acme/web", "main", "b", "t", "b", [])
    assert err.value.status == 422 and "Validation Failed" in err.value.body


def test_5xx_gives_up_after_retries(gh, api):
    route = api.get(f"{R}/git/ref/heads/x").respond(503)
    with pytest.raises(VcsError):
        gh.find_branch("acme/web", "x")
    assert route.call_count == 4


@pytest.mark.parametrize("status, expected", [("ahead", True), ("identical", True), ("behind", False),
                                              ("diverged", False)])
def test_is_ancestor_uses_compare(gh, api, status, expected):
    sha = "a" * 40
    api.get(f"{R}/compare/{sha}...main").respond(200, json={"status": status})
    assert gh.is_ancestor("acme/web", sha, "main") is expected


def test_is_ancestor_unknown_sha(gh, api):
    api.get(f"{R}/compare/{'b' * 40}...main").respond(404, json={"message": "Not Found"})
    assert gh.is_ancestor("acme/web", "b" * 40, "main") is False


def test_close_comment_only_from_repo_members(gh, api):
    api.get(f"{R}/issues/4/comments").respond(200, json=[
        {"body": "member says no", "user": {"type": "User"}, "author_association": "MEMBER"},
        {"body": "outsider instruction", "user": {"type": "User"}, "author_association": "NONE"},
        {"body": "contributor", "user": {"type": "User"}, "author_association": "CONTRIBUTOR"},
    ])
    assert gh.close_comment("acme/web", 4) == "member says no"


def test_get_pr(gh, api):
    api.get(f"{R}/pulls/9").respond(200, json=pr(9, "closed", "2026-01-01"))
    got = gh.get_pr("acme/web", 9)
    assert got.number == 9 and got.state == "merged"


def test_push_branch_walks_tree_when_listing_truncated(gh, api):
    api.post(f"{R}/git/blobs").respond(201, json={"sha": "blob1"})
    api.get(f"{R}/git/commits/base").respond(200, json={"tree": {"sha": "tree0"}})
    api.get(f"{R}/git/trees/tree0", params={"recursive": "1"}).respond(200, json={"tree": [], "truncated": True})
    api.get(f"{R}/git/trees/tree0").respond(200, json={"tree": [
        {"path": "scripts", "mode": "040000", "type": "tree", "sha": "treeS"}]})
    api.get(f"{R}/git/trees/treeS").respond(200, json={"tree": [
        {"path": "release.sh", "mode": "100755", "type": "blob", "sha": "x"}]})
    trees = api.post(f"{R}/git/trees").respond(201, json={"sha": "tree1"})
    api.post(f"{R}/git/commits").respond(201, json={"sha": "c1"})
    api.post(f"{R}/git/refs").respond(201, json={})
    gh.push_branch("acme/web", "b", "base", "m", [FileChange(path="scripts/release.sh", content="echo\n"),
                                                  FileChange(path="new/x.py", content="x\n")])
    entries = {e["path"]: e["mode"] for e in body(trees.calls[0])["tree"]}
    assert entries == {"scripts/release.sh": "100755", "new/x.py": "100644"}


def test_post_5xx_not_retried(gh, api):
    route = api.post(f"{R}/pulls").respond(502)
    with pytest.raises(VcsError):
        gh.open_pr("acme/web", "main", "b", "t", "b", [])
    assert route.call_count == 1
