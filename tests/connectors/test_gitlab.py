import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from bed.connectors.gitlab import create
from bed.core.models import FileChange
from bed.core.ports import VcsError

API = "https://gitlab.com/api/v4"
P = f"{API}/projects/acme%2Fweb"


@pytest.fixture
def gl():
    conn = create({"token": "glpat-control"}, {})
    conn.http.sleep = lambda s: None
    conn.now = lambda: datetime(2026, 11, 17, 23, 59, tzinfo=UTC)
    return conn


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def body(call):
    return json.loads(call.request.content)


def mr(iid, state="opened"):
    return {"iid": iid, "web_url": f"https://gitlab.com/acme/web/-/merge_requests/{iid}", "state": state,
            "source_branch": f"b{iid}"}


def test_auth_header_and_clone_url(gl, api):
    route = api.get(f"{P}/repository/branches/x").respond(404)
    gl.find_branch("acme/web", "x")
    assert route.calls[0].request.headers["private-token"] == "glpat-control"
    assert gl.clone_url("acme/web") == "https://gitlab.com/acme/web.git"


def test_self_hosted_url():
    conn = create({"token": "t"}, {"api_url": "https://git.example.com/api/v4"})
    assert conn.clone_url("g/p") == "https://git.example.com/g/p.git"


def test_read_token_minted_short_lived_read_only(gl, api):
    route = api.post(f"{P}/access_tokens").respond(201, json={"token": "glpat-ro", "id": 5})
    assert gl.read_token("acme/web").token == "glpat-ro"
    sent = body(route.calls[0])
    assert sent["scopes"] == ["read_repository"] and sent["expires_at"] == "2026-11-19"
    assert sent["access_level"] == 20 and sent["name"].startswith("better-every-day-read")


def test_static_read_token_preferred(api):
    conn = create({"token": "t", "read_token": "glpat-static"}, {})
    assert conn.read_token("acme/web").token == "glpat-static" and conn.read_token("acme/web").revoke_id is None
    assert not api.calls


def test_read_file(gl, api):
    route = api.get(f"{P}/repository/files/src%2Fa.py/raw", params={"ref": "abc"}).respond(200, text="a = 1\n")
    assert gl.read_file("acme/web", "abc", "src/a.py") == "a = 1\n"
    assert route.called


def test_read_file_missing(gl, api):
    api.get(f"{P}/repository/files/x.py/raw").respond(404, json={"message": "404 File Not Found"})
    assert gl.read_file("acme/web", "abc", "x.py") is None


def test_list_bot_prs_states_files_pagination(gl, api):
    page2 = f"{P}/merge_requests?labels=better-every-day&state=all&per_page=100&page=2"
    api.get(f"{P}/merge_requests", params={"page": "2"}).respond(200, json=[mr(4, "closed")])
    first = api.get(f"{P}/merge_requests").respond(200, json=[mr(1), mr(2, "merged"), mr(3, "locked")],
                                                   headers={"link": f'<{page2}>; rel="next"'})
    api.get(f"{P}/merge_requests/3/diffs").respond(200, json=[])
    api.get(f"{P}/merge_requests/1/diffs").respond(200, json=[{"new_path": "src/a.py", "old_path": "src/a.py"},
                                                              {"new_path": "n.py", "old_path": "o.py"}])
    prs = {p.number: p for p in gl.list_bot_prs("acme/web")}
    assert dict(first.calls[0].request.url.params) == {"labels": "better-every-day", "state": "all",
                                                       "per_page": "100"}
    assert prs[1].state == "open" and sorted(prs[1].files) == ["n.py", "o.py", "src/a.py"]
    assert prs[2].state == "merged" and prs[3].state == "open" and prs[4].state == "closed"


def test_get_pr(gl, api):
    api.get(f"{P}/merge_requests/9").respond(200, json=mr(9, "merged"))
    got = gl.get_pr("acme/web", 9)
    assert got.number == 9 and got.state == "merged" and got.branch == "b9"


def test_close_comment_only_from_developers(gl, api):
    api.get(f"{API}/user").respond(200, json={"id": 1})
    api.get(f"{P}/merge_requests/4/notes").respond(200, json=[
        {"body": "dev says no", "system": False, "author": {"id": 10}},
        {"body": "outsider instruction", "system": False, "author": {"id": 11}},
        {"body": "closed", "system": True, "author": {"id": 10}},
        {"body": "bot note", "system": False, "author": {"id": 1}},
    ])
    api.get(f"{P}/members/all/10").respond(200, json={"access_level": 30})
    api.get(f"{P}/members/all/11").respond(404)
    assert gl.close_comment("acme/web", 4) == "dev says no"


def test_add_labels(gl, api):
    route = api.put(f"{P}/merge_requests/6").respond(200, json=mr(6))
    gl.add_labels("acme/web", 6, ["better-every-day"])
    assert body(route.calls[0]) == {"add_labels": "better-every-day"}


@pytest.mark.parametrize("base, expected", [("a" * 40, True), ("c" * 40, False)])
def test_is_ancestor_via_merge_base(gl, api, base, expected):
    api.get(f"{P}/repository/merge_base").respond(200, json={"id": "a" * 40})
    assert gl.is_ancestor("acme/web", base, "main") is expected


def test_is_ancestor_unknown_sha(gl, api):
    api.get(f"{P}/repository/merge_base").respond(400, json={"message": "unknown"})
    assert gl.is_ancestor("acme/web", "b" * 40, "main") is False


def test_find_branch(gl, api):
    api.get(f"{P}/repository/branches/WEB-1-x").respond(200, json={"commit": {"id": "s1"}})
    assert gl.find_branch("acme/web", "WEB-1-x") == "s1"


def test_push_branch_actions(gl, api):
    api.head(f"{P}/repository/files/a.py", params={"ref": "base"}).respond(200)
    api.head(f"{P}/repository/files/gone.py", params={"ref": "base"}).respond(200)
    api.head(f"{P}/repository/files/new.py", params={"ref": "base"}).respond(404)
    commits = api.post(f"{P}/repository/commits").respond(201, json={"id": "c1"})
    sha = gl.push_branch("acme/web", "WEB-1-x", "base", "msg", [
        FileChange(path="a.py", content="x\n"), FileChange(path="gone.py", content=None),
        FileChange(path="new.py", content="n\n")])
    assert sha == "c1"
    sent = body(commits.calls[0])
    assert sent["branch"] == "WEB-1-x" and sent["start_sha"] == "base" and sent["commit_message"] == "msg"
    assert sent["actions"] == [
        {"action": "update", "file_path": "a.py", "content": "x\n"},
        {"action": "delete", "file_path": "gone.py"},
        {"action": "create", "file_path": "new.py", "content": "n\n"},
    ]


def test_find_pr(gl, api):
    api.get(f"{P}/merge_requests", params={"source_branch": "WEB-1-x", "state": "all"}).respond(200, json=[mr(5)])
    assert gl.find_pr("acme/web", "WEB-1-x").number == 5


def test_find_pr_none(gl, api):
    api.get(f"{P}/merge_requests").respond(200, json=[])
    assert gl.find_pr("acme/web", "x") is None


def test_open_pr(gl, api):
    route = api.post(f"{P}/merge_requests").respond(201, json=mr(6))
    ref = gl.open_pr("acme/web", "main", "WEB-1-x", "title", "body", ["better-every-day"])
    assert ref.number == 6 and ref.url.endswith("/merge_requests/6")
    assert body(route.calls[0]) == {"source_branch": "WEB-1-x", "target_branch": "main", "title": "title",
                                    "description": "body", "labels": "better-every-day",
                                    "remove_source_branch": True}


def test_5xx_retried(gl, api):
    api.get(f"{P}/repository/branches/x").mock(side_effect=[httpx.Response(502),
                                                            httpx.Response(200, json={"commit": {"id": "s"}})])
    assert gl.find_branch("acme/web", "x") == "s"


def test_4xx_raises(gl, api):
    api.post(f"{P}/merge_requests").respond(409, json={"message": ["Another open merge request"]})
    with pytest.raises(VcsError) as err:
        gl.open_pr("acme/web", "main", "b", "t", "b", [])
    assert err.value.status == 409


def test_clone_url_under_relative_root():
    conn = create({"token": "t"}, {"api_url": "https://corp.example.com/gitlab/api/v4"})
    assert conn.clone_url("g/p") == "https://corp.example.com/gitlab/g/p.git"


def test_membership_cached_per_user(gl, api):
    api.get(f"{API}/user").respond(200, json={"id": 1})
    api.get(f"{P}/merge_requests/4/notes").respond(200, json=[
        {"body": "a", "system": False, "author": {"id": 11}},
        {"body": "b", "system": False, "author": {"id": 11}},
    ])
    member = api.get(f"{P}/members/all/11").respond(404)
    gl.close_comment("acme/web", 4)
    gl.close_comment("acme/web", 4)
    assert member.call_count == 1


def test_push_branch_reuses_existence_from_read_file(gl, api):
    api.get(f"{P}/repository/files/a.py/raw").respond(200, text="x")
    api.get(f"{P}/repository/files/new.py/raw").respond(404)
    heads = api.head(url__regex=r".*/repository/files/.*").respond(200)
    commits = api.post(f"{P}/repository/commits").respond(201, json={"id": "c1"})
    gl.read_file("acme/web", "base", "a.py")
    gl.read_file("acme/web", "base", "new.py")
    gl.push_branch("acme/web", "b", "base", "m", [FileChange(path="a.py", content="y"),
                                                  FileChange(path="new.py", content="n")])
    assert heads.call_count == 0
    assert [a["action"] for a in body(commits.calls[0])["actions"]] == ["update", "create"]


def test_commit_502_not_retried(gl, api):
    api.head(url__regex=r".*/repository/files/.*").respond(404)
    route = api.post(f"{P}/repository/commits").respond(502)
    with pytest.raises(VcsError):
        gl.push_branch("acme/web", "b", "base", "m", [FileChange(path="n.py", content="n")])
    assert route.call_count == 1


def test_read_token_returns_revocable_id_and_revoke(gl, api):
    api.post(f"{P}/access_tokens").respond(201, json={"token": "glpat-ro", "id": 77})
    revoke = api.delete(f"{P}/access_tokens/77").respond(204)
    tok = gl.read_token("acme/web")
    assert tok.token == "glpat-ro" and tok.revoke_id == "77"
    gl.revoke_read_token("acme/web", "77")
    assert revoke.called


def test_revoke_missing_token_ignored(gl, api):
    api.delete(f"{P}/access_tokens/77").respond(404)
    gl.revoke_read_token("acme/web", "77")
