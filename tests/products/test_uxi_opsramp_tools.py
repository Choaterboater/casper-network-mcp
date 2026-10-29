"""UXI and OpsRamp tools against a recording fake: login, paths, query encoding, dry runs, the read-only pin.

Sample data, not from a real network.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Backend, body_of

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products.opsramp import tools as opsramp
from casper_network_mcp.products.opsramp.client import OpsRampClient
from casper_network_mcp.products.uxi import tools as uxi
from casper_network_mcp.products.uxi.client import UxiClient

UXI = "/networking-uxi/v1alpha1"
OPSRAMP_BASE = "https://acme.api.opsramp.com"
OPSRAMP_TOKEN = "/tenancy/auth/oauth/token"
TENANT = "client_00001"
T = f"/api/v2/tenants/{TENANT}"
SECRET = "test-secret-do-not-log"


@pytest.fixture
def uxi_backend(recording_transport):
    def make(read_only: bool = False) -> Backend:
        client = UxiClient(gate=Gate(read_only), client_id="id", client_secret=SECRET, transport=recording_transport)
        return Backend(uxi.backend(client=lambda: client))

    return make


@pytest.fixture
def opsramp_backend(recording_transport):
    def make(read_only: bool = False) -> Backend:
        client = OpsRampClient(
            gate=Gate(read_only),
            base_url=f"{OPSRAMP_BASE}/api/",
            tenant_id=TENANT,
            client_id="key",
            client_secret=SECRET,
            transport=recording_transport,
        )
        return Backend(opsramp.backend(client=lambda: client))

    return make


def _api_calls(transport):
    return transport.calls  # the fake keeps login traffic in token_calls


# ── UXI ─────────────────────────────────────────────────────────────────────


async def test_uxi_lists_with_a_greenlake_token(uxi_backend, recording_transport):
    recording_transport.reply({"items": [{"id": "s1", "name": "Lobby"}], "count": 1, "next": None}, "GET", f"{UXI}/sensors")
    out = await uxi_backend().call("uxi_list_sensors", {"limit": 500})
    assert out["items"][0]["name"] == "Lobby"
    [call] = recording_transport.calls
    assert str(call.url) == f"https://api.capenetworks.com{UXI}/sensors?limit=100"  # clamped to UXI's largest page
    assert call.headers["Authorization"] == "Bearer tok-1"
    assert len(recording_transport.token_calls) == 1


async def test_uxi_status_overview_covers_sensors_and_agents(uxi_backend, recording_transport):
    recording_transport.reply({"items": [{"id": "s1", "name": "Lobby", "groupName": "Home"}]}, "GET", f"{UXI}/sensors")
    recording_transport.reply({"items": [{"id": "a1", "name": "Laptop"}]}, "GET", f"{UXI}/agents")
    recording_transport.reply({"isOnline": True, "isTesting": True, "issues": []}, "GET", f"{UXI}/sensors/s1/status")
    issue = {"code": "DNS_LOOKUP_FAILED", "severity": "ERROR", "status": "CONFIRMED", "timestamp": "t",
             "context": {"networkName": "Example"}}  # fmt: skip
    recording_transport.reply({"isOnline": False, "isTesting": False, "issues": [issue]}, "GET", f"{UXI}/sensors/a1/status")
    out = await uxi_backend().call("uxi_status_overview", {})
    assert out["counts"] == {"listed": 2, "checked": 2, "online": 1, "offline": 1, "with_issues": 1}
    agent = out["items"][1]
    assert agent["type"] == "agent" and agent["issues"][0]["code"] == "DNS_LOOKUP_FAILED"
    assert agent["issues"][0]["network"] == "Example"


async def test_uxi_changes_use_merge_patch_and_dry_run_sends_nothing(uxi_backend, recording_transport):
    b = uxi_backend()
    preview = await b.call("uxi_update_sensor", {"sensor_id": "s1", "name": "Front desk", "dry_run": True})
    assert preview == {"would_send": {"method": "PATCH", "path": f"{UXI}/sensors/s1", "body": {"name": "Front desk"}}}
    assert recording_transport.calls == []
    await b.call("uxi_update_sensor", {"sensor_id": "s1", "name": "Front desk"})
    [call] = recording_transport.calls
    assert call.method == "PATCH" and call.headers["Content-Type"] == "application/merge-patch+json"
    assert body_of(call) == {"name": "Front desk"}
    await b.call("uxi_assign_to_group", {"kind": "agent", "item_id": "a1", "group_id": "g1"})
    assert recording_transport.calls[-1].url.path == f"{UXI}/agent-group-assignments"
    assert body_of(recording_transport.calls[-1]) == {"agentId": "a1", "groupId": "g1"}


async def test_uxi_refuses_bad_ids_and_empty_changes(uxi_backend, recording_transport):
    b = uxi_backend()
    assert "error" in await b.call("uxi_get_status", {"sensor_id": "../agents"})
    assert "error" in await b.call("uxi_update_agent", {"agent_id": "a1"})
    assert recording_transport.calls == []


async def test_uxi_read_only_server_refuses_changes(uxi_backend, recording_transport):
    b = uxi_backend(read_only=True)
    out = await b.call("uxi_delete_group", {"group_id": "g1"})
    assert "read-only" in out["error"]
    assert recording_transport.calls == []


async def test_uxi_without_a_login_sends_nothing(recording_transport):
    client = UxiClient(gate=Gate(False), client_id=None, client_secret=None, transport=recording_transport)
    out = await Backend(uxi.backend(client=lambda: client)).call("uxi_list_agents", {})
    assert out == {"error": "login_missing", "product": "uxi"}
    assert recording_transport.calls == [] and recording_transport.token_calls == []


# ── OpsRamp ─────────────────────────────────────────────────────────────────


async def test_opsramp_logs_in_on_the_tenant_host_with_a_form(opsramp_backend, recording_transport):
    recording_transport.reply({"results": [{"id": 1001}], "totalResults": 1}, "GET", f"{T}/alerts/search")
    out = await opsramp_backend().call("opsramp_search_alerts", {})
    assert out["results"] == [{"id": 1001}]
    [login] = recording_transport.token_calls
    assert str(login.url) == f"{OPSRAMP_BASE}{OPSRAMP_TOKEN}"
    assert b"grant_type=client_credentials" in login.content and b"client_id=key" in login.content
    [call] = _api_calls(recording_transport)
    assert call.headers["Authorization"] == "Bearer tok-1"
    assert "queryString=states:CRITICAL,WARNING&pageNo=1&pageSize=50" in str(call.url)


async def test_opsramp_query_space_goes_as_percent_20_not_plus(opsramp_backend, recording_transport):
    """OpsRamp reads + as its field separator, so "On Hold" must not become "On+Hold"."""
    recording_transport.reply({"results": [], "totalResults": 0}, "GET", f"{T}/incidents/search")
    await opsramp_backend().call("opsramp_search_incidents", {})
    url = str(_api_calls(recording_transport)[0].url)
    assert "status:New,Open,Pending,On%20Hold" in url and "+" not in url


async def test_opsramp_overview_counts(opsramp_backend, recording_transport):
    def by_query(request: httpx.Request):
        q = request.url.params.get("queryString")
        return {"states:CRITICAL": {"totalResults": 2}, "states:WARNING": {"totalResults": 5}}.get(q, {"totalResults": 7})

    for sub in ("/alerts/search", "/incidents/search", "/resources/search"):
        recording_transport.reply(by_query, "GET", f"{T}{sub}")
    out = await opsramp_backend().call("opsramp_overview", {})
    assert out == {"critical_alerts": 2, "warning_alerts": 5, "open_incidents": 7, "active_resources": 7}


async def test_opsramp_alert_action_posts_once_and_dry_run_sends_nothing(opsramp_backend, recording_transport):
    b = opsramp_backend()
    args = {"alert_id": "1001", "action": "suppress", "comment": "maintenance", "snooze_minutes": 120}
    preview = await b.call("opsramp_alert_action", {**args, "dry_run": True})
    assert preview["would_send"]["path"] == f"{T}/alerts/1001/actions/suppress"
    assert _api_calls(recording_transport) == []
    await b.call("opsramp_alert_action", args)
    [call] = _api_calls(recording_transport)
    assert call.method == "POST" and call.url.path == f"{T}/alerts/1001/actions/suppress"
    assert body_of(call) == {"description": "maintenance", "snoozeDuration": 120}
    await b.call("opsramp_alert_action", {"alert_id": "1001", "action": "unacknowledge"})
    assert _api_calls(recording_transport)[-1].url.path.endswith("/actions/unAcknowledge")


async def test_opsramp_refuses_bad_ids_snooze_without_suppress_and_read_only(opsramp_backend, recording_transport):
    b = opsramp_backend()
    assert "error" in await b.call("opsramp_get_alert", {"alert_id": "1/../2"})
    assert "error" in await b.call("opsramp_alert_action", {"alert_id": "1", "action": "close", "snooze_minutes": 5})
    out = await opsramp_backend(read_only=True).call("opsramp_alert_action", {"alert_id": "1", "action": "close"})
    assert "read-only" in out["error"]
    assert _api_calls(recording_transport) == []


async def test_opsramp_without_a_tenant_sends_nothing(recording_transport):
    client = OpsRampClient(
        gate=Gate(False), base_url=OPSRAMP_BASE, tenant_id=None, client_id="key", client_secret=SECRET,
        transport=recording_transport,
    )  # fmt: skip
    out = await Backend(opsramp.backend(client=lambda: client)).call("opsramp_search_alerts", {})
    assert out == {"error": "login_missing", "product": "opsramp"}
    assert recording_transport.calls == [] and recording_transport.token_calls == []


async def test_401_renews_the_token_once_for_a_read(opsramp_backend, recording_transport):
    replies = iter([401, {"results": [], "totalResults": 0}])
    recording_transport.reply(lambda request: next(replies), "GET", f"{T}/resources/search")
    out = await opsramp_backend().call("opsramp_search_resources", {})
    assert out["totalResults"] == 0
    assert len(recording_transport.token_calls) == 2
