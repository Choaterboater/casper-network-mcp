"""Product clients: logins from the process environment, one gated request path each."""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest
from conftest import CENTRAL_BASE, TOKEN_URL, body_of

from casper_network_mcp.core.gate import Gate, LoginMissing, Refused
from casper_network_mcp.products.central.client import CentralClient
from casper_network_mcp.products.clearpass.client import ClearPassClient
from casper_network_mcp.products.mist.client import ApiError, MistClient

SRC = Path(__file__).resolve().parents[2] / "src" / "casper_network_mcp"
LOGIN_VARS = {
    "MIST_HOST",
    "MIST_API_TOKEN",
    "CENTRAL_BASE_URL",
    "CENTRAL_CLIENT_ID",
    "CENTRAL_CLIENT_SECRET",
    "CLEARPASS_BASE_URL",
    "CLEARPASS_API_TOKEN",
}


# ── Logins ──────────────────────────────────────────────────────────────────


def test_only_the_login_module_reads_the_environment():
    readers = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                readers.add(path.relative_to(SRC).as_posix())
    # specs_index.py reads only LOCALAPPDATA, in cache_dir; test_boundary.py holds it to exactly that.
    assert readers <= {"core/logins.py", "specs_index.py"}, readers


def test_login_module_reads_only_login_variables():
    from casper_network_mcp.core import logins

    assert set(logins.LOGIN_VARS) == LOGIN_VARS


async def test_mist_login_from_env(monkeypatch, recording_transport):
    monkeypatch.setenv("MIST_API_TOKEN", "abc")
    monkeypatch.delenv("MIST_HOST", raising=False)
    client = MistClient.from_env(Gate(False), transport=recording_transport)
    monkeypatch.setenv("MIST_API_TOKEN", "changed-later")  # read once, at start
    await client.request("GET", "/api/v1/self")
    req = recording_transport.calls[0]
    assert req.headers["Authorization"] == "Token abc"
    assert str(req.url) == "https://api.mist.com/api/v1/self"


async def test_mist_host_can_be_a_bare_name_or_a_url(monkeypatch, recording_transport):
    monkeypatch.setenv("MIST_API_TOKEN", "abc")
    for host in ("api.eu.mist.com", "https://api.eu.mist.com"):
        monkeypatch.setenv("MIST_HOST", host)
        await MistClient.from_env(Gate(False), transport=recording_transport).request("GET", "/api/v1/self")
        assert recording_transport.calls[-1].url.host == "api.eu.mist.com"


async def test_a_bad_product_address_is_refused_before_sending(monkeypatch, recording_transport):
    monkeypatch.setenv("MIST_API_TOKEN", "abc")
    monkeypatch.setenv("MIST_HOST", "http://api.mist.com")  # plain http off this machine
    client = MistClient.from_env(Gate(False), transport=recording_transport)
    with pytest.raises(Refused, match="address"):
        await client.request("GET", "/api/v1/self")
    assert recording_transport.calls == []


@pytest.mark.parametrize(
    "cls,missing",
    [
        (MistClient, ["MIST_API_TOKEN"]),
        (CentralClient, ["CENTRAL_CLIENT_SECRET"]),
        (ClearPassClient, ["CLEARPASS_BASE_URL"]),
    ],
)
async def test_each_product_reports_its_missing_login(monkeypatch, cls, missing):
    for var in LOGIN_VARS:
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("CENTRAL_BASE_URL", CENTRAL_BASE)
    monkeypatch.setenv("CLEARPASS_BASE_URL", "https://198.51.100.20")
    for var in missing:
        monkeypatch.delenv(var)
    client = cls.from_env(Gate(False))
    with pytest.raises(LoginMissing) as info:
        await client.request("GET", client.probe_path)
    assert info.value.as_error() == {"error": "login_missing", "product": client.product}


# ── Errors ──────────────────────────────────────────────────────────────────


async def test_api_error_shape(recording_transport, mist_client_factory):
    recording_transport.reply(
        httpx.Response(404, json={"detail": "site not found"}, headers={"x-request-id": "req-1"}),
        "GET",
        "/api/v1/sites/s9",
    )
    client = mist_client_factory(gate=Gate(False), transport=recording_transport)
    with pytest.raises(ApiError) as info:
        await client.request("GET", "/api/v1/sites/s9")
    err = info.value.as_error()
    assert err["status"] == 404 and err["detail"] == "site not found" and err["request_id"] == "req-1"
    assert err["url"] == "https://api.mist.com/api/v1/sites/s9"
    assert "404" in err["error"] and "Mist" in err["error"]


async def test_html_error_body_is_summarized(recording_transport, mist_client_factory):
    html = b"<!doctype html>\n<html><head><title>Not Found</title></head><body>nope</body></html>"
    recording_transport.reply(
        httpx.Response(404, content=html, headers={"content-type": "text/html"}),
        "GET",
        "/api/v1/sites/s9/stats/devices/abc",
    )
    client = mist_client_factory(gate=Gate(False), transport=recording_transport)
    with pytest.raises(ApiError) as info:
        await client.request("GET", "/api/v1/sites/s9/stats/devices/abc")
    err = info.value.as_error()
    assert err["detail"] == f"Not Found (HTML body, {len(html)} bytes)"
    assert "<html" not in str(err["error"])


async def test_error_detail_hides_secrets(recording_transport, mist_client_factory):
    recording_transport.reply((400, {"detail": {"psk": "s3cret", "why": "too short"}}))
    client = mist_client_factory(gate=Gate(False), transport=recording_transport)
    with pytest.raises(ApiError) as info:
        await client.request("PUT", "/api/v1/sites/s1/psks/p1", json={"psk": "s3cret"})
    assert "s3cret" not in str(info.value.as_error())


async def test_writes_are_not_retried(recording_transport, mist_client_factory):
    recording_transport.reply(503)
    client = mist_client_factory(gate=Gate(False), transport=recording_transport)
    with pytest.raises(ApiError):
        await client.request("POST", "/api/v1/sites/s1/wlans", json={"ssid": "Guest"})
    assert len(recording_transport.calls) == 1


async def test_empty_reply_is_none(recording_transport, mist_client_factory):
    recording_transport.reply((204, None))
    client = mist_client_factory(gate=Gate(False), transport=recording_transport)
    assert await client.request("DELETE", "/api/v1/sites/s1/wlans/w1") is None


async def test_login_headers_cannot_be_overridden(recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(False), transport=recording_transport)
    await client.request("GET", "/api/v1/self", headers={"Authorization": "Token evil", "If-Match": "1"})
    req = recording_transport.calls[0]
    assert req.headers["Authorization"] == "Token test-token" and req.headers["If-Match"] == "1"


# ── ClearPass ──────────────────────────────────────────────────────────────


async def test_clearpass_bearer_and_api_paths(recording_transport, clearpass_client_factory):
    client = clearpass_client_factory(gate=Gate(False), transport=recording_transport)
    await client.request("GET", "/api/oauth/me")
    req = recording_transport.calls[0]
    assert req.headers["Authorization"] == "Bearer test-token"
    assert str(req.url) == "https://198.51.100.20/api/oauth/me"
    with pytest.raises(ValueError):
        await client.request("GET", "/oauth/me")  # ClearPass paths live under /api


# ── Central ─────────────────────────────────────────────────────────────────


async def test_central_gets_a_token_once_and_sends_it(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(False), transport=recording_transport)
    await client.request("GET", "/network-monitoring/v1/aps")
    await client.request("GET", "/network-monitoring/v1/aps")
    assert len(recording_transport.token_calls) == 1
    form = recording_transport.token_calls[0].content.decode()
    assert "grant_type=client_credentials" in form and "client_id=test-id" in form
    assert str(recording_transport.token_calls[0].url) == TOKEN_URL
    assert recording_transport.calls[0].headers["Authorization"] == "Bearer tok-1"


async def test_central_refreshes_the_token_once_on_401(recording_transport, central_client_factory):
    seen = []

    def reply(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"ok": True})

    recording_transport.reply(reply)
    client = central_client_factory(gate=Gate(False), transport=recording_transport)
    assert await client.request("GET", "/network-monitoring/v1/aps") == {"ok": True}
    assert seen == ["Bearer tok-1", "Bearer tok-2"]


async def test_central_paths_stay_in_the_central_api(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(False), transport=recording_transport)
    with pytest.raises(ValueError):
        await client.request("GET", "/platform/workspaces/v1/x")
    assert recording_transport.calls == []


def test_central_path_prefixes_match_the_spec():
    from casper_network_mcp.products.central.client import CENTRAL_PREFIXES
    from casper_network_mcp.specs_bundle import operations

    assert set(CENTRAL_PREFIXES) == {"/" + op.path.split("/")[1] + "/" for op in operations("central")}


def test_central_sync_front_door_uses_the_same_gate(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    with pytest.raises(Refused, match="read-only"):
        client.request_sync("PUT", "/network-config/v1/layer2-vlan/30", json={"vlan": 30})
    assert recording_transport.calls == []
    recording_transport.reply({"items": [{"serial": "SG1"}]})
    out = client.request_sync("GET", "/network-monitoring/v1/aps", params={"limit": 5})
    assert out == {"items": [{"serial": "SG1"}]}
    assert recording_transport.calls[0].url.params["limit"] == "5"
    assert recording_transport.calls[0].headers["Authorization"].startswith("Bearer ")
    with pytest.raises(ValueError):
        client.request_sync("GET", "/network-monitoring/v1/../../x")


def test_central_sync_sends_writes_once_and_hides_secrets(recording_transport, central_client_factory):
    recording_transport.reply((503, {"detail": "busy"}))
    client = central_client_factory(gate=Gate(read_only=False), transport=recording_transport)
    with pytest.raises(ApiError):
        client.request_sync("POST", "/network-config/v1/layer2-vlan/30", json={"vlan": 30})
    assert len(recording_transport.calls) == 1
    assert body_of(recording_transport.calls[0]) == {"vlan": 30}
    recording_transport.reply({"shared-secret": "abc", "name": "radius-1"})
    out = client.request_sync("GET", "/network-config/v1/auth-servers/radius-1")
    assert out["shared-secret"] == "[hidden]"


def test_central_sync_login_missing(monkeypatch):
    for var in ("CENTRAL_BASE_URL", "CENTRAL_CLIENT_ID", "CENTRAL_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(LoginMissing):
        CentralClient.from_env(Gate(False)).request_sync("GET", "/network-monitoring/v1/aps")
