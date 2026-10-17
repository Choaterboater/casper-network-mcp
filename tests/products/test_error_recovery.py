"""Recovery contracts across product clients, with synthetic APIs only."""

from __future__ import annotations

import httpx
import pytest

from casper_network_mcp.core import http as http_mod
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products._base import ApiError, TransportFailure


@pytest.fixture(params=["mist", "central", "central-sync", "clearpass", "clearpass-renewable"])
async def error_client(
    request, recording_transport, mist_client_factory, central_client_factory, clearpass_client_factory
):
    factories = {
        "mist": mist_client_factory,
        "central": central_client_factory,
        "central-sync": central_client_factory,
        "clearpass": clearpass_client_factory,
    }
    if request.param == "clearpass-renewable":
        from conftest import CLEARPASS_BASE

        from casper_network_mcp.products.clearpass.client import ClearPassClient

        client = ClearPassClient(
            gate=Gate(False),
            base_url=CLEARPASS_BASE,
            client_id="test-id",
            client_secret="test-secret",
            transport=recording_transport,
        )
        recording_transport.reply({"access_token": "test-token", "expires_in": 7200}, "POST", "/api/oauth")
    else:
        client = factories[request.param](gate=Gate(False), transport=recording_transport)

    async def send(method, path=None, **kwargs):
        path = path or client.probe_path
        if request.param == "central-sync":
            return client.request_sync(method, path, **kwargs)
        return await client.request(method, path, **kwargs)

    yield client, send
    await client.aclose()


@pytest.fixture
def no_retry_wait(monkeypatch):
    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(http_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr("casper_network_mcp.products.central.client.time.sleep", lambda seconds: None)


async def test_403_reports_permission_code_and_role_guidance(error_client, recording_transport):
    _, send = error_client
    recording_transport.reply(403)
    with pytest.raises(ApiError) as info:
        await send("GET")
    out = info.value.as_error()
    assert out["code"] == "permission_denied"
    assert out["guidance"] == "Check the login's role and access to this target before trying again."


async def test_exhausted_429_reports_rate_limit_and_retry_after(error_client, recording_transport, no_retry_wait):
    client, send = error_client
    recording_transport.reply(httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "3"}))
    with pytest.raises(ApiError) as info:
        await send("GET")
    out = info.value.as_error()
    assert out["code"] == "rate_limited"
    assert out["guidance"] == (
        "Wait for the rate limit to clear. Retry this read after checking connectivity and service availability."
    )
    assert out["retry_after_seconds"] == 3
    assert len([call for call in recording_transport.calls if call.url.path == client.probe_path]) == 3


async def test_write_timeout_reports_unknown_outcome(error_client, recording_transport):
    client, send = error_client

    def timeout(request):
        raise httpx.ReadTimeout("response timed out", request=request)

    recording_transport.reply(timeout, "PUT", client.probe_path)
    with pytest.raises(TransportFailure) as info:
        await send("PUT", json={"name": "test"})
    out = info.value.as_error()
    assert out["code"] == "timeout"
    assert out["status"] is None
    assert out["guidance"] == ("The outcome is unknown; check the target's current state before retrying this change.")


async def test_read_timeout_guidance_allows_read_retry(error_client, recording_transport, no_retry_wait):
    client, send = error_client

    def timeout(request):
        raise httpx.ReadTimeout("response timed out", request=request)

    recording_transport.reply(timeout, "GET", client.probe_path)
    with pytest.raises(TransportFailure) as info:
        await send("GET")
    assert info.value.as_error()["guidance"] == (
        "Retry this read after checking connectivity and service availability."
    )


async def test_connection_error_has_distinct_code(error_client, recording_transport):
    client, send = error_client

    def disconnected(request):
        raise httpx.ConnectError("connection refused", request=request)

    recording_transport.reply(disconnected, "PUT", client.probe_path)
    with pytest.raises(TransportFailure) as info:
        await send("PUT", json={})
    assert info.value.as_error()["code"] == "connection_error"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("failure", [401, 429, 503, "timeout"])
async def test_each_failed_write_is_sent_once(method, failure, error_client, recording_transport):
    client, send = error_client

    def reply(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("response timed out", request=request)
        return httpx.Response(failure, json={"detail": "request refused"})

    recording_transport.reply(reply, method, client.probe_path)
    with pytest.raises((ApiError, TransportFailure)):
        await send(method, json={"name": "test"})
    assert [
        (call.method, call.url.path) for call in recording_transport.calls if call.url.path == client.probe_path
    ] == [(method, client.probe_path)]


async def test_api_error_preserves_existing_fields(error_client, recording_transport):
    client, send = error_client
    recording_transport.reply(
        httpx.Response(
            404,
            json={"detail": "not found"},
            headers={"x-request-id": "req-1"},
        )
    )
    with pytest.raises(ApiError) as info:
        await send("GET")
    out = info.value.as_error()
    assert {key: out[key] for key in ("error", "status", "detail", "request_id", "url")} == {
        "error": f"{client.product.capitalize() if client.product != 'clearpass' else 'ClearPass'} answered 404"
        f" to GET {client.base_url}{client.probe_path}: not found",
        "status": 404,
        "detail": "not found",
        "request_id": "req-1",
        "url": f"{client.base_url}{client.probe_path}",
    }


async def test_recovery_error_fields_do_not_leak_secrets(error_client, recording_transport):
    _, send = error_client
    recording_transport.reply((403, {"detail": {"password": "synthetic-private-value", "reason": "denied"}}))
    with pytest.raises(ApiError) as info:
        await send("GET")
    out = info.value.as_error()
    assert "synthetic-private-value" not in repr(out)
    assert out["detail"]["password"] == "[hidden]"


async def test_transport_error_detail_hides_credentials(error_client, recording_transport):
    client, send = error_client

    def timeout(request):
        raise httpx.ReadTimeout("upstream: Bearer synthetic-private-credential", request=request)

    recording_transport.reply(timeout, "PUT", client.probe_path)
    with pytest.raises(TransportFailure) as info:
        await send("PUT", json={})
    out = info.value.as_error()
    assert "synthetic-private-credential" not in repr(out)
    assert "[hidden]" in out["detail"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("mist_get", {"path": "/api/v1/sites/s1"}),
        ("mist_get_site_info", {"site_id": "s1"}),
    ],
)
async def test_router_preserves_recovery_fields_for_curated_and_generated_tools(name, args, server_with_replies):
    server = server_with_replies({("GET", "/api/v1/sites/s1"): 403})
    out = await server.call("invoke_read_tool", {"name": name, "arguments": args})
    assert out["code"] == "permission_denied"
    assert out["guidance"] == "Check the login's role and access to this target before trying again."


@pytest.mark.parametrize(
    "name,args,path",
    [
        ("mist_set_marvis_settings", {"org_id": "o1", "settings": {}}, "/api/v1/orgs/o1/setting"),
        ("mist_update_site_info", {"site_id": "s1", "body": {"name": "test"}}, "/api/v1/sites/s1"),
    ],
)
async def test_router_preserves_unknown_write_timeout(name, args, path, server_with_replies):
    def timeout(request):
        raise httpx.ReadTimeout("response timed out", request=request)

    server = server_with_replies({("PUT", path): timeout})
    out = await server.call("invoke_tool", {"name": name, "arguments": args})
    assert out["code"] == "timeout"
    assert out["guidance"] == "The outcome is unknown; check the target's current state before retrying this change."
