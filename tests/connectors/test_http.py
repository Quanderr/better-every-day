import httpx
import pytest
import respx

from bed.connectors.http import HttpApi


class Boom(Exception):
    def __init__(self, status, body):
        super().__init__(status)
        self.status = status


@pytest.fixture
def http():
    api = HttpApi("https://api.test", {}, Boom)
    api.slept = []
    api.sleep = api.slept.append
    return api


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE"])
def test_idempotent_methods_retry_5xx(http, method):
    with respx.mock() as mock:
        route = mock.route(method=method, url="https://api.test/x").mock(
            side_effect=[httpx.Response(502), httpx.Response(200)])
        http.send(method, "/x")
    assert route.call_count == 2


def test_post_not_retried_on_5xx(http):
    with respx.mock() as mock:
        route = mock.post("https://api.test/x").respond(502)
        with pytest.raises(Boom):
            http.send("POST", "/x")
    assert route.call_count == 1


def test_post_retried_on_429_honouring_retry_after(http):
    with respx.mock() as mock:
        route = mock.post("https://api.test/x").mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(201)])
        http.send("POST", "/x")
    assert route.call_count == 2 and http.slept == [7]


def test_retry_after_capped(http):
    with respx.mock() as mock:
        mock.get("https://api.test/x").mock(side_effect=[
            httpx.Response(503, headers={"Retry-After": "3600"}), httpx.Response(200)])
        http.send("GET", "/x")
    assert http.slept == [60]


def test_per_call_headers_merge(http):
    http.headers = {"A": "1"}
    with respx.mock() as mock:
        route = mock.get("https://api.test/x").respond(200)
        http.send("GET", "/x", headers={"B": "2"})
    h = route.calls[0].request.headers
    assert h["a"] == "1" and h["b"] == "2"
