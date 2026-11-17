import json

import pytest
import respx

from bed.connectors.jira import create
from bed.connectors.jira.connector import to_adf
from bed.core.ports import TrackerError

BASE = "https://acme.atlassian.net"


@pytest.fixture
def jira():
    conn = create({"email": "bot@acme.test", "api_token": "tok"}, {"url": BASE})
    conn.http.sleep = lambda s: None
    return conn


def body(call):
    return json.loads(call.request.content)


def test_create_ticket_fields_labels_and_basic_auth(jira):
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/rest/api/3/issue").respond(201, json={"key": "WEB-12"})
        ref = jira.create_ticket("WEB", "Fix it", "## Problem\nBody", "fp123")
    assert ref.id == "WEB-12" and ref.url == f"{BASE}/browse/WEB-12"
    req = route.calls[0].request
    assert req.headers["authorization"].startswith("Basic ")
    fields = body(route.calls[0])["fields"]
    assert fields["project"] == {"key": "WEB"} and fields["summary"] == "Fix it"
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["labels"] == ["better-every-day", "bed-fp-fp123"]
    assert fields["description"]["type"] == "doc" and "bed-fingerprint" not in json.dumps(fields["description"])


def test_issue_type_option():
    conn = create({"email": "e", "api_token": "t"}, {"url": BASE, "issue_type": "Bug"})
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/rest/api/3/issue").respond(201, json={"key": "WEB-1"})
        conn.create_ticket("WEB", "t", "b", "fp")
    assert body(route.calls[0])["fields"]["issuetype"] == {"name": "Bug"}


def test_find_ticket_by_label(jira):
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/rest/api/3/search/jql").respond(200, json={"issues": [{"key": "WEB-3"}]})
        ref = jira.find_ticket("WEB", "fp1")
    assert ref.id == "WEB-3" and ref.url == f"{BASE}/browse/WEB-3"
    sent = body(route.calls[0])
    assert sent["jql"] == 'project = "WEB" AND labels = "bed-fp-fp1"' and sent["maxResults"] == 1


def test_find_ticket_none(jira):
    with respx.mock() as mock:
        mock.post(f"{BASE}/rest/api/3/search/jql").respond(200, json={"issues": []})
        assert jira.find_ticket("WEB", "fp1") is None


def test_find_ticket_rejects_quote_injection(jira):
    with pytest.raises(TrackerError):
        jira.find_ticket('WEB" OR project = "X', "fp1")


def test_link_pr_idempotent_remote_link(jira):
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/rest/api/3/issue/WEB-12/remotelink").respond(201, json={"id": 1})
        jira.link_pr("WEB-12", "https://github.com/acme/web/pull/1", "PR title")
    assert body(route.calls[0]) == {"globalId": "https://github.com/acme/web/pull/1",
                                    "object": {"url": "https://github.com/acme/web/pull/1", "title": "PR title"}}


def test_errors_raise(jira):
    with respx.mock() as mock, pytest.raises(TrackerError, match="Field 'x' is required"):
        mock.post(f"{BASE}/rest/api/3/issue").respond(400, json={"errorMessages": ["Field 'x' is required"]})
        jira.create_ticket("WEB", "t", "b", "fp")


def test_url_required():
    with pytest.raises(KeyError):
        create({"email": "e", "api_token": "t"}, {})


def test_to_adf_headings_paragraphs_code():
    doc = to_adf("## Problem\n```text\nx = @a\n```\n\nplain line\nsecond\n\n## Files\n- `a.py`\n- `b.py`")
    kinds = [n["type"] for n in doc["content"]]
    assert kinds == ["heading", "codeBlock", "paragraph", "heading", "bulletList"]
    assert doc["content"][1]["content"][0]["text"] == "x = @a"
    para = doc["content"][2]["content"]
    assert [n["type"] for n in para] == ["text", "hardBreak", "text"]
    items = doc["content"][4]["content"]
    assert len(items) == 2
    code = items[0]["content"][0]["content"][0]
    assert code == {"type": "text", "text": "a.py", "marks": [{"type": "code"}]}


def test_to_adf_hash_without_space_is_text():
    doc = to_adf("#include <stdio.h>\n#123 regressed")
    assert [n["type"] for n in doc["content"]] == ["paragraph"]
    assert doc["content"][0]["content"][0]["text"] == "#include <stdio.h>"


def test_to_adf_unterminated_fence_does_not_swallow():
    doc = to_adf("```text\ncode line\n\n## After")
    assert doc["content"][-1]["type"] == "heading"


def test_to_adf_decodes_neutralised_markdown():
    doc = to_adf("ping @\u200bteam &lt;b&gt; &amp; !\u200b[x](u)")
    text = doc["content"][0]["content"][0]["text"]
    assert text == "ping @team <b> & ![x](u)"


@pytest.mark.parametrize("title, expected", [("Fix\nit  now", "Fix it now"), ("   \n ", "better-every-day finding"),
                                             ("x" * 300, "x" * 250)])
def test_summary_sanitised(jira, title, expected):
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/rest/api/3/issue").respond(201, json={"key": "WEB-1"})
        jira.create_ticket("WEB", title, "b", "fp")
    assert body(route.calls[0])["fields"]["summary"] == expected


def test_create_502_not_retried(jira):
    with respx.mock() as mock:
        route = mock.post(f"{BASE}/rest/api/3/issue").respond(502)
        with pytest.raises(TrackerError):
            jira.create_ticket("WEB", "t", "b", "fp")
    assert route.call_count == 1
