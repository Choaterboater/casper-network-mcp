"""The gate: the one check every product request passes before anything is sent."""

from __future__ import annotations

import pytest
from conftest import first_write_tool, placeholder_args

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.gate import Gate, LoginMissing, Refused

PING_PATH = "/network-troubleshooting/v1/cx/SG12345678/ping"  # on TROUBLESHOOT_OPS
PORT_BOUNCE_PATH = "/network-troubleshooting/v1/cx/SG12345678/portBounce"
DOWNLOAD_LINK = "/network-reporting/v1/reports/r1/report-runs/r2/download-link"  # on READ_POSTS


async def test_read_only_refuses_writes_before_sending(recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(read_only=True, scopes={}), transport=recording_transport)
    with pytest.raises(Refused, match="read-only"):
        await client.request("PUT", "/api/v1/sites/s1/wlans/w1", json={"vlan_id": 30})
    assert recording_transport.calls == []


async def test_read_only_allows_listed_read_posts(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=True, scopes={}), transport=recording_transport)
    await client.request("POST", DOWNLOAD_LINK, json={"exportType": "CSV"})
    assert recording_transport.calls[0].method == "POST"


async def test_read_only_refuses_a_post_that_is_not_on_the_read_list(recording_transport, mist_client_factory):
    # Mist's alarm search is a GET in its spec; a POST to the same path is not a reviewed read.
    client = mist_client_factory(gate=Gate(read_only=True, scopes={}), transport=recording_transport)
    with pytest.raises(Refused, match="read-only"):
        await client.request("POST", "/api/v1/orgs/o1/alarms/search", json={})
    with pytest.raises(Refused, match="read-only"):
        await client.request("POST", "/api/v1/orgs/o1/alarms/search", json={}, kind="read")
    assert recording_transport.calls == []


async def test_read_only_allows_plain_gets(recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    await client.request("GET", "/api/v1/orgs/o1/alarms/search")
    assert [c.method for c in recording_transport.calls] == ["GET"]


async def test_read_only_refuses_a_get_that_changes_something(recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    with pytest.raises(Refused, match="read-only"):
        await client.request("GET", "/api/v1/installer/sites/Branch-12/optimize")
    with pytest.raises(Refused, match="read-only"):
        await client.request("GET", "/api/v1/sites/s1/wlans", kind="config")
    assert recording_transport.calls == []


async def test_a_declared_kind_never_loosens_the_gate(recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    with pytest.raises(Refused):
        await client.request("PUT", "/api/v1/sites/s1/wlans/w1", json={}, kind="read")
    with pytest.raises(Refused):
        await client.request("POST", "/api/v1/sites/s1/devices/d1/pingsweep", json={}, kind="troubleshoot")
    assert recording_transport.calls == []


async def test_no_login_is_login_missing(monkeypatch):
    from casper_network_mcp.products.mist.client import LoginMissing as MistLoginMissing
    from casper_network_mcp.products.mist.client import MistClient

    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    with pytest.raises(MistLoginMissing) as info:
        await MistClient.from_env(Gate(False, {})).request("GET", "/api/v1/self")
    assert info.value.product == "mist"
    assert info.value.as_error() == {"error": "login_missing", "product": "mist"}
    assert MistLoginMissing is LoginMissing


async def test_secret_values_are_hidden(recording_transport, mist_client_factory):
    recording_transport.reply({"results": [{"name": "Guest", "passphrase": "x1"}]})
    client = mist_client_factory(gate=Gate(True, {}), transport=recording_transport)
    out = await client.request("GET", "/api/v1/orgs/o1/psks")
    assert out["results"][0]["passphrase"] == "[hidden]"


async def test_read_only_lets_listed_troubleshooting_through(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    await client.request("POST", PING_PATH, json={"destination": "192.0.2.1"}, kind="troubleshoot")
    assert recording_transport.calls[-1].method == "POST"
    with pytest.raises(Refused, match="read-only"):
        await client.request("POST", PORT_BOUNCE_PATH, json={}, kind="disruptive")
    assert len(recording_transport.calls) == 1


async def test_troubleshooting_runs_only_as_troubleshooting(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    with pytest.raises(Refused):
        await client.request("POST", PING_PATH, json={}, kind="config")
    assert recording_transport.calls == []


@pytest.mark.parametrize("product", ["central", "mist", "clearpass"])
async def test_generated_write_is_refused_before_sending(product, recording_transport, generated_backend_factory):
    backend = generated_backend_factory(product, gate=Gate(read_only=True), transport=recording_transport)
    name = first_write_tool(product)
    out = await backend.call(name, placeholder_args(sdk_compat.get_tool(backend.server, name)))
    assert "read-only" in out["error"] and recording_transport.calls == []


async def test_path_piece_that_moves_the_request_is_refused_on_an_approved_write(
    recording_transport, generated_backend_factory
):
    backend = generated_backend_factory("mist", gate=Gate(read_only=False), transport=recording_transport)
    out = await backend.call(
        "mist_update_site_wlan", {"site_id": "abc/../../orgs/X", "wlan_id": "w1", "body": {"vlan_id": 30}}
    )
    assert "site_id" in out["error"] and "Nothing was sent" in out["error"]
    assert recording_transport.calls == []


async def test_unsafe_raw_path_is_refused_before_sending(recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(read_only=False), transport=recording_transport)
    for path in ("/api/v1/sites/../orgs/X", "/api/v1/sites/s1?x=1", "https://api.mist.com/api/v1/self", "/other/x"):
        with pytest.raises(ValueError):
            await client.request("GET", path)
    assert recording_transport.calls == []


async def test_writes_pass_when_not_read_only(recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(read_only=False), transport=recording_transport)
    await client.request("PUT", "/api/v1/sites/s1/wlans/w1", json={"vlan_id": 30})
    assert recording_transport.calls[0].method == "PUT"


def test_gate_check_directly():
    gate = Gate(read_only=True)
    gate.check("mist", "GET", "/api/v1/self", {}, "read")
    with pytest.raises(Refused) as info:
        gate.check("mist", "DELETE", "/api/v1/sites/s1", {"site_id": "s1"}, "delete")
    assert info.value.as_error()["error"].startswith("Not sent")
    with pytest.raises(ValueError):
        gate.check("mist", "GET", "/api/v1/self", {}, "write")
    Gate(read_only=False).check("mist", "DELETE", "/api/v1/sites/s1", {}, "delete")
