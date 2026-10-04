import asyncio

import httpx
import pytest

from casper_network_mcp.core import http as http_mod
from casper_network_mcp.core.http import Http, compact_http_error, parse_retry_after


class Recorder:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status, json={"ok": status == 200}, headers={"Retry-After": "0"})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def fast(_seconds):
        return None

    monkeypatch.setattr(http_mod.asyncio, "sleep", fast)


async def test_get_is_retried_on_429():
    rec = Recorder([429, 503, 200])
    h = Http("t", transport=httpx.MockTransport(rec))
    resp = await h.request("GET", "https://api.mist.com/api/v1/self")
    assert resp.status_code == 200 and len(rec.calls) == 3
    await h.aclose()


async def test_writes_are_sent_once():
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        rec = Recorder([429])
        h = Http("t", transport=httpx.MockTransport(rec))
        resp = await h.request(method, "https://api.mist.com/api/v1/x", json={"a": 1})
        assert resp.status_code == 429 and len(rec.calls) == 1, method
        await h.aclose()


async def test_params_headers_and_body_are_sent():
    rec = Recorder([200])
    h = Http("t", transport=httpx.MockTransport(rec))
    await h.request("POST", "https://api.mist.com/x", headers={"X-A": "1"}, params={"q": "v"}, json={"b": 2})
    req = rec.calls[0]
    assert req.headers["X-A"] == "1" and req.url.params["q"] == "v" and req.content == b'{"b":2}'
    await h.aclose()


async def test_each_instance_has_its_own_client_and_pool_is_drained():
    a = Http("same", transport=httpx.MockTransport(Recorder([200])))
    b = Http("same", transport=httpx.MockTransport(Recorder([200])))
    assert a.client() is not b.client()
    clients = [a.client(), b.client()]
    await http_mod.aclose_pooled_clients()
    assert all(c.is_closed for c in clients)


def test_parse_retry_after():
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("") is None
    assert parse_retry_after("nonsense") is None
    assert parse_retry_after("-5") == 0.0


def test_compact_http_error():
    resp = httpx.Response(403, json={"detail": "no"})
    assert compact_http_error(resp, endpoint="/api/v1/self").startswith("HTTP 403 at /api/v1/self:")
    long = httpx.Response(500, text="x" * 1000)
    assert "cut 760 characters" in compact_http_error(long)


def test_pooled_client_is_per_loop():
    async def get():
        return http_mod.pooled_client("loop-test")

    first = asyncio.run(get())
    second = asyncio.run(get())
    assert first is not second
