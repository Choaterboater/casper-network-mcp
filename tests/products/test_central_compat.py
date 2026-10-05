"""The Central compatibility layer: the copied tools' ``get_client()`` and helpers, over the gated client.

Covers the shim (``_request``/``get``/``post``... all through ``request_sync`` or
``exchange``), the lifted SSID and scope-map helpers, and the scope-id check.
"""

from __future__ import annotations

import ast
import pathlib

import httpx
import pytest
from conftest import body_of

from casper_network_mcp.core.gate import Gate, Refused
from casper_network_mcp.core.scope_ids import normalize_scope_id
from casper_network_mcp.products._tools import use_client
from casper_network_mcp.products.central import compat, scope_maps, ssid

PING = "/network-troubleshooting/v1/cx/SN1/ping"


@pytest.fixture
def central(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=False), transport=recording_transport)
    use_client("central", lambda: client)
    return compat.get_client()


@pytest.fixture
def central_read_only(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    use_client("central", lambda: client)
    return compat.get_client()


def test_request_keeps_the_status_and_hides_secrets(central, recording_transport):
    recording_transport.reply((404, {"detail": "no such thing", "password": "x"}), "GET", "/network-config/v1/global")
    resp = central._request("GET", "/network-config/v1/global")
    assert resp.status_code == 404 and not resp.is_success
    assert "no such thing" in resp.text and "x" not in resp.json().get("password", "")


def test_get_raises_with_the_reply_on_an_error(central, recording_transport):
    recording_transport.reply((409, {"detail": "duplicate entry"}), "POST", "/network-config/v1/layer2-vlan/30")
    with pytest.raises(compat.CentralHTTPError) as caught:
        central.post("/network-config/v1/layer2-vlan/30", data={"vlan": 30})
    assert "duplicate" in caught.value.response.text
    assert caught.value.as_error()["status"] == 409


def test_list_replies_become_items(central, recording_transport):
    recording_transport.reply([{"id": 1}], "GET", "/network-config/v1alpha1/wlan-ssids")
    assert central.get("/network-config/v1alpha1/wlan-ssids") == {"items": [{"id": 1}]}


def test_post_async_returns_the_location(central, recording_transport):
    recording_transport.reply(
        httpx.Response(202, headers={"Location": "/x/async-operations/t1"}, json={}), "POST", PING
    )
    assert central.post_async(PING, data={"destination": "192.0.2.1"}) == "/x/async-operations/t1"


def test_read_only_pin_refuses_a_write_through_the_shim(central_read_only, recording_transport):
    with pytest.raises(Refused):
        central_read_only._request("POST", "/network-config/v1/layer2-vlan/30", json={})
    assert recording_transport.calls == []


def test_diagnostic_means_troubleshoot(central_read_only, recording_transport):
    central_read_only._request("POST", PING, json={"destination": "192.0.2.1"}, diagnostic=True)
    assert recording_transport.calls[-1].url.path == PING
    with pytest.raises(Refused):  # declaring a bounce a check does not make it one
        central_read_only._request("POST", "/network-troubleshooting/v1/cx/SN1/poeBounce", json={}, diagnostic=True)


async def test_async_request_goes_through_the_same_gate(central_read_only, recording_transport):
    with pytest.raises(Refused):
        await central_read_only._arequest("POST", "/network-troubleshooting/v1/cx/SN1/poeBounce", json={})
    resp = await central_read_only._arequest("POST", PING, json={}, diagnostic=True)
    assert resp.status_code == 200
    assert await central_read_only.aget("/network-config/v1/global") == {"ok": True}


async def test_troubleshoot_polls_until_completed(central, recording_transport, monkeypatch):
    monkeypatch.setattr(compat, "POLL_INTERVAL", 0)
    recording_transport.reply(httpx.Response(202, headers={"Location": "/a/async-operations/t9"}), "POST", PING)
    recording_transport.reply({"status": "COMPLETED", "output": "5 packets"}, "GET", f"{PING}/async-operations/t9")
    errors: list[str] = []
    out = await compat.atroubleshoot_async(
        central,
        compat.troubleshooting_endpoint_candidates("cx", "SN1", "ping"),
        {"destination": "192.0.2.1"},
        errors,
        diagnostic=True,
    )
    assert out["status"] == "COMPLETED" and out["endpoint_used"] == PING


def test_troubleshooting_endpoints_are_the_bundled_v1_only():
    assert compat.troubleshooting_endpoint_candidates("cx", "SN1", "ping") == [PING]
    with pytest.raises(ValueError):
        compat.troubleshooting_endpoint_candidates("cx", "SN1/../x", "ping")


def test_scope_ids_are_digits_only():
    assert normalize_scope_id(" 123 ") == "123"
    for bad in ("", "12a", "-1", True, None, "1" * 21):
        with pytest.raises(ValueError):
            normalize_scope_id(bad)


def test_scope_map_body(central, recording_transport):
    scope_maps._post_scope_map(central, "42", "ACCESS_SWITCH", "layer2-vlan/30")
    call = recording_transport.calls[0]
    # Task 10: scope-maps is in no bundled document; config-assignments is.
    assert call.url.path == "/network-config/v1alpha1/config-assignments"
    assert body_of(call) == {
        "config-assignment": [
            {
                "scope-id": "42",
                "device-function": "ACCESS_SWITCH",
                "profile-type": "layer2-vlan",
                "profile-instance": "30",
            }
        ]
    }


def test_global_scope_id(central, recording_transport):
    recording_transport.reply({"scopeId": "7"}, "GET", "/network-config/v1/global")
    assert scope_maps._fetch_global_scope_id(central) == "7"


def test_vlan_interface_updates_when_create_fails(central, recording_transport):
    recording_transport.reply((400, {"detail": "conflict"}), "POST", "/network-config/v1alpha1/layer2-vlan/30")
    vi = {"vlan": 30, "ip_address": None, "helper_address": None, "dhcp": True}
    scope_maps._push_vlan_interface(central, vi, "11", "7", "ACCESS_SWITCH")
    sent = [(c.method, c.url.path) for c in recording_transport.calls]
    assert ("PUT", "/network-config/v1alpha1/layer2-vlan/30") in sent
    assert sent[-1] == ("POST", "/network-config/v1alpha1/config-assignments")


def test_device_profiles_use_the_named_switch_group(central, recording_transport):
    recording_transport.reply({"scopeId": "7"}, "GET", "/network-config/v1/global")
    recording_transport.reply(
        {"items": [{"scopeName": "Core Switches", "scopeId": "9"}]}, "GET", "/network-config/v1/device-groups"
    )
    errors = scope_maps._ensure_device_profiles(central, switch_group_name="Core Switches")
    assert errors == []
    scopes = {
        body_of(c)["config-assignment"][0]["scope-id"]
        for c in recording_transport.calls
        if c.url.path == "/network-config/v1alpha1/config-assignments"
    }
    assert scopes == {"7", "9"}


def test_ssid_list_and_get(central, recording_transport):
    recording_transport.reply({"items": [{"essid": {"name": "Guest"}}]}, "GET", "/network-config/v1alpha1/wlan-ssids")
    assert ssid.list_underlay_ssids(central)
    ssid.get_underlay_ssid(central, "Guest Net")
    assert recording_transport.calls[-1].url.raw_path.endswith(b"/wlan-ssids/Guest%20Net")


def test_no_pipeline_imports():
    import casper_network_mcp.products.central as pkg

    for p in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        mods = {
            n.module
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
            if isinstance(n, ast.ImportFrom) and n.module
        }
        assert not any(
            m and ("pipeline" in m or "stages" in m or "vlan_loader" in m or "state_store" in m) for m in mods
        ), p.name
