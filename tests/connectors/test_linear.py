import json

import httpx
import pytest
import respx

from bed.connectors.linear import create
from bed.core.ports import TrackerError

URL = "https://api.linear.app/graphql"


class Fake:
    def __init__(self, label_exists=True, errors=None, issues=()):
        self.label_exists = label_exists
        self.errors = errors
        self.issues = list(issues)
        self.requests = []

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        q = payload["query"]
        if self.errors:
            return httpx.Response(200, json={"errors": self.errors})
        if "teams(" in q:
            return httpx.Response(200, json={"data": {"teams": {"nodes": [{"id": "team-1"}]}}})
        if "issueLabelCreate" in q:
            return httpx.Response(200, json={"data": {"issueLabelCreate": {"success": True,
                                                                           "issueLabel": {"id": "label-new"}}}})
        if "issueLabels(" in q:
            nodes = [{"id": "label-1"}] if self.label_exists else []
            return httpx.Response(200, json={"data": {"issueLabels": {"nodes": nodes}}})
        if "issueCreate" in q:
            return httpx.Response(200, json={"data": {"issueCreate": {"success": True, "issue": {
                "identifier": "WEB-12", "url": "https://linear.app/x/issue/WEB-12"}}}})
        if "issues(" in q:
            return httpx.Response(200, json={"data": {"issues": {"nodes": self.issues}}})
        if "attachmentLinkURL" in q:
            return httpx.Response(200, json={"data": {"attachmentLinkURL": {"success": True}}})
        raise AssertionError(q)

    def find(self, word):
        return [r for r in self.requests if word in r["query"]]


@pytest.fixture
def linear():
    return create({"api_key": "lin_key"}, {})


def run(fake, fn):
    with respx.mock() as mock:
        route = mock.post(URL).mock(side_effect=fake)
        out = fn()
    return out, route


def test_create_sends_team_label_and_marker(linear):
    fake = Fake()
    ref, route = run(fake, lambda: linear.create_ticket("WEB", "Fix it", "Body", "fp123"))
    assert ref.id == "WEB-12" and ref.url.endswith("WEB-12")
    inp = fake.find("issueCreate")[0]["variables"]["input"]
    assert inp["teamId"] == "team-1" and inp["labelIds"] == ["label-1"] and inp["title"] == "Fix it"
    assert inp["description"].endswith("bed-fingerprint: fp123")
    assert route.calls[0].request.headers["authorization"] == "lin_key"
    assert fake.find("issueLabelCreate") == []


def test_label_created_when_missing(linear):
    fake = Fake(label_exists=False)
    run(fake, lambda: linear.create_ticket("WEB", "t", "b", "fp"))
    assert fake.find("issueCreate")[0]["variables"]["input"]["labelIds"] == ["label-new"]
    assert fake.find("issueLabelCreate")[0]["variables"]["input"] == {"name": "better-every-day",
                                                                      "teamId": "team-1"}


def test_team_and_label_cached(linear):
    fake = Fake()
    run(fake, lambda: (linear.create_ticket("WEB", "t", "b", "a"), linear.create_ticket("WEB", "t", "b", "b")))
    assert len(fake.find("teams(")) == 1 and len(fake.find("issueLabels(")) == 1


def test_find_returns_none_on_empty(linear):
    fake = Fake()
    out, _ = run(fake, lambda: linear.find_ticket("WEB", "fp1"))
    assert out is None
    f = fake.find("issues(")[0]["variables"]["filter"]
    assert f == {"team": {"key": {"eq": "WEB"}}, "description": {"contains": "bed-fingerprint: fp1"}}


def test_find_returns_first(linear):
    fake = Fake(issues=[{"identifier": "WEB-3", "url": "u3"}])
    out, _ = run(fake, lambda: linear.find_ticket("WEB", "fp1"))
    assert out.id == "WEB-3" and out.url == "u3"


def test_link_pr(linear):
    fake = Fake()
    run(fake, lambda: linear.link_pr("WEB-12", "https://github.com/acme/web/pull/1", "PR"))
    assert fake.find("attachmentLinkURL")[0]["variables"] == {
        "issueId": "WEB-12", "url": "https://github.com/acme/web/pull/1", "title": "PR"}


def test_errors_raise(linear):
    with pytest.raises(TrackerError, match="bad"):
        run(Fake(errors=[{"message": "bad"}]), lambda: linear.find_ticket("WEB", "fp"))


def test_non_200_raises(linear):
    with respx.mock() as mock, pytest.raises(TrackerError):
        mock.post(URL).respond(500, text="down")
        linear.find_ticket("WEB", "fp")
