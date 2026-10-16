"""ClearPass logs in by itself with an API client (client credentials) and renews its token."""

from __future__ import annotations

import types

import httpx
import pytest
from conftest import CLEARPASS_BASE, body_of

from casper_network_mcp.core.gate import Gate, LoginMissing
from casper_network_mcp.products.clearpass import client as cp_client
from casper_network_mcp.products.clearpass.client import ApiError, ClearPassClient

SECRET = "synthetic-client-secret"
TOKEN_URL = f"{CLEARPASS_BASE}/api/oauth"


class FakeClearPass(httpx.MockTransport):
    """Answers ``POST /api/oauth`` with a new token each time; records everything else."""

    def __init__(self, token_reply=None) -> None:
        super().__init__(self._handle)
        self.token_calls: list[httpx.Request] = []
        self.calls: list[httpx.Request] = []
        self.token_reply = token_reply
        self.api_reply = lambda request: httpx.Response(200, json={"info": "API operator", "name": "api-client"})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == TOKEN_URL:
            self.token_calls.append(request)
            if self.token_reply is not None:
                return self.token_reply
            n = len(self.token_calls)
            return httpx.Response(200, json={"access_token": f"cp-{n}", "token_type": "Bearer", "expires_in": 3600})
        self.calls.append(request)
        return self.api_reply(request)


def _client(transport, **kw):
    kw.setdefault("client_id", "casper-client")
    kw.setdefault("client_secret", SECRET)
    return ClearPassClient(gate=Gate(False), base_url=CLEARPASS_BASE, transport=transport, **kw)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cp_client, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


async def test_token_fetched_with_a_json_body_and_cached():
    fake = FakeClearPass()
    client = _client(fake)
    await client.request("GET", "/api/oauth/me")
    await client.request("GET", "/api/oauth/me")
    assert len(fake.token_calls) == 1
    login = fake.token_calls[0]
    assert login.headers["Content-Type"] == "application/json"
    assert body_of(login) == {"grant_type": "client_credentials", "client_id": "casper-client", "client_secret": SECRET}
    assert [r.headers["Authorization"] for r in fake.calls] == ["Bearer cp-1", "Bearer cp-1"]


async def test_probe_works_with_the_fetched_token():
    fake = FakeClearPass()
    client = _client(fake)
    assert client.has_login
    assert await client.request("GET", client.probe_path) == {"info": "API operator", "name": "api-client"}
    assert str(fake.calls[0].url) == f"{CLEARPASS_BASE}/api/oauth/me"


async def test_token_renewed_before_it_runs_out(clock):
    fake = FakeClearPass()
    client = _client(fake)
    await client.request("GET", "/api/oauth/me")
    clock[0] += 3600 - 61  # still inside the early-refresh margin
    await client.request("GET", "/api/oauth/me")
    assert len(fake.token_calls) == 1
    clock[0] += 2  # within a minute of expiry: renew
    await client.request("GET", "/api/oauth/me")
    assert len(fake.token_calls) == 2
    assert fake.calls[-1].headers["Authorization"] == "Bearer cp-2"


async def test_401_forgets_the_token_and_retries_once():
    fake = FakeClearPass()
    seen = []

    def reply(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"ok": True})

    fake.api_reply = reply
    client = _client(fake)
    assert await client.request("GET", "/api/oauth/me") == {"ok": True}
    assert seen == ["Bearer cp-1", "Bearer cp-2"]
    assert len(fake.token_calls) == 2


async def test_a_second_401_is_login_expired_and_not_retried_again():
    fake = FakeClearPass()
    fake.api_reply = lambda request: httpx.Response(401, json={"detail": "nope"})
    client = _client(fake)
    with pytest.raises(ApiError) as info:
        await client.request("GET", "/api/oauth/me")
    assert info.value.as_error() == {"error": "login_expired", "product": "clearpass"}
    assert len(fake.calls) == 2 and len(fake.token_calls) == 2


@pytest.mark.parametrize(
    "reply,text",
    [
        (httpx.Response(400, json={"error": "invalid_client", "client_secret": SECRET}), "login was refused"),
        (httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600}), "reply had no token"),
        (httpx.Response(200, json={"access_token": "", "expires_in": 3600}), "reply had no token"),
        (httpx.Response(200, content=b"<html>login</html>"), "reply had no token"),
    ],
)
async def test_login_errors_are_plain_and_never_carry_the_secret(reply, text):
    fake = FakeClearPass(token_reply=reply)
    client = _client(fake)
    with pytest.raises(ApiError) as info:
        await client.request("GET", "/api/oauth/me")
    err = info.value
    assert text in str(err) and "ClearPass" in str(err)
    for shown in (str(err), repr(err), str(err.as_error()), repr(client)):
        assert SECRET not in shown
    assert fake.calls == []  # nothing reached the API


async def test_the_pasted_api_token_wins_when_both_are_set():
    fake = FakeClearPass()
    client = _client(fake, token="pasted-token")
    await client.request("GET", "/api/oauth/me")
    assert fake.token_calls == []
    assert fake.calls[0].headers["Authorization"] == "Bearer pasted-token"


async def test_from_env_reads_the_api_client(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", CLEARPASS_BASE)
    monkeypatch.setenv("CLEARPASS_CLIENT_ID", "casper-client")
    monkeypatch.setenv("CLEARPASS_CLIENT_SECRET", SECRET)
    fake = FakeClearPass()
    client = ClearPassClient.from_env(Gate(False), transport=fake)
    assert client.has_login
    await client.request("GET", "/api/oauth/me")
    assert body_of(fake.token_calls[0])["client_id"] == "casper-client"
    monkeypatch.setenv("CLEARPASS_API_TOKEN", "pasted-token")
    await ClearPassClient.from_env(Gate(False), transport=fake).request("GET", "/api/oauth/me")
    assert len(fake.token_calls) == 1
    assert fake.calls[-1].headers["Authorization"] == "Bearer pasted-token"


async def test_an_id_without_a_secret_is_no_login(monkeypatch):
    monkeypatch.setenv("CLEARPASS_BASE_URL", CLEARPASS_BASE)
    monkeypatch.setenv("CLEARPASS_CLIENT_ID", "casper-client")
    fake = FakeClearPass()
    client = ClearPassClient.from_env(Gate(False), transport=fake)
    assert not client.has_login
    with pytest.raises(LoginMissing):
        await client.request("GET", "/api/oauth/me")
    assert fake.token_calls == [] and fake.calls == []


async def test_read_only_refuses_a_change_before_any_login():
    fake = FakeClearPass()
    client = ClearPassClient(
        gate=Gate(read_only=True),
        base_url=CLEARPASS_BASE,
        client_id="casper-client",
        client_secret=SECRET,
        transport=fake,
    )
    with pytest.raises(Exception, match="read-only"):
        await client.request("DELETE", "/api/endpoint/1")
    assert fake.token_calls == [] and fake.calls == []
