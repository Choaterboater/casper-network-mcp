"""Cable tests require the change route; on/off toggles and NAC MACs stay config."""

from __future__ import annotations

import httpx
import pytest
from conftest import CENTRAL_BASE
from test_labels import casper_word_kind

from casper_network_mcp.core import kinds
from casper_network_mcp.core.gate import Gate, Refused
from casper_network_mcp.core.kinds import TROUBLESHOOT_OPS, kind_for_operation, labels
from casper_network_mcp.products import _tools
from casper_network_mcp.products.central import compat
from casper_network_mcp.router import find
from casper_network_mcp.server import build_server

CABLE_OPERATIONS = [
    ("central", "/network-troubleshooting/v1/cx/SG12345678/cableTest"),
    ("central", "/network-troubleshooting/v1/aos-s/SG12345678/cableTest"),
    ("mist", "/api/v1/sites/s1/devices/d1/cable_test"),
]
CABLE_CALLS = [
    pytest.param(
        "cable_test", {"serial_number": "SG12345678", "ports": ["1/1/1"], "device_type": "CX"},
        CABLE_OPERATIONS[0][1], id="curated-cx",
    ),
    pytest.param(
        "cable_test", {"serial_number": "SG12345678", "ports": ["1"], "device_type": "AOS-S"},
        CABLE_OPERATIONS[1][1], id="curated-aos-s",
    ),
    pytest.param(
        "central_initiate_cx_cable_test_v1", {"serial_number": "SG12345678", "body": {"ports": ["1/1/1"]}},
        CABLE_OPERATIONS[0][1], id="generated-cx",
    ),
    pytest.param(
        "central_initiate_pvos_cable_test_v1", {"serial_number": "SG12345678", "body": {"ports": ["1"]}},
        CABLE_OPERATIONS[1][1], id="generated-aos-s",
    ),
    pytest.param(
        "mist_cable_test_from_switch", {"site_id": "s1", "device_id": "d1", "body": {"port": "ge-0/0/0"}},
        CABLE_OPERATIONS[2][1], id="generated-mist",
    ),
]


@pytest.fixture
async def cable_server(monkeypatch, recording_transport):
    monkeypatch.setattr(_tools, "_getters", dict(_tools._getters))
    monkeypatch.setattr(compat, "POLL_INTERVAL", 0)
    monkeypatch.setattr(compat, "POLL_MAX", 1)
    servers = []

    def make(read_only):
        server = build_server(
            read_only=read_only,
            transport=recording_transport,
            environ={
                "CENTRAL_BASE_URL": CENTRAL_BASE,
                "CENTRAL_CLIENT_ID": "test-id",
                "CENTRAL_CLIENT_SECRET": "test-secret",
                "MIST_API_TOKEN": "test-token",
            },
        )
        servers.append(server)
        return server

    yield make
    for server in servers:
        for client in server.clients.values():
            await client.aclose()


@pytest.mark.parametrize("product,path", CABLE_OPERATIONS)
def test_cable_operation_has_disruptive_override(product, path):
    template = kinds.listed_template("POST", path)
    assert ("POST", template) not in TROUBLESHOOT_OPS
    assert kind_for_operation("POST", path) == "disruptive"
    assert "briefly takes the tested port's link down" in kinds.KIND_OVERRIDES[("POST", template)][1]


@pytest.mark.parametrize("name,args,path", CABLE_CALLS)
def test_cable_discovery_is_disruptive(name, args, path):
    hit = next(hit for hit in find.find_tool(name, top_k=10) if hit["name"] == name)
    assert hit["kind"] == "disruptive"
    assert hit["label"] == "destructive"


@pytest.mark.parametrize("name,args,path", CABLE_CALLS)
async def test_cable_read_route_refuses_before_http(name, args, path, cable_server, recording_transport):
    server = cable_server(False)
    out = await server.call("invoke_read_tool", {"name": name, "arguments": args})
    assert out["error"] == "not_a_read_tool"
    assert recording_transport.calls == []
    assert recording_transport.token_calls == []


@pytest.mark.parametrize("name,args,path", CABLE_CALLS)
async def test_cable_change_route_is_blocked_in_read_only(name, args, path, cable_server, recording_transport):
    server = cable_server(True)
    out = await server.call("invoke_tool", {"name": name, "arguments": args})
    assert "read-only" in out["error"]
    assert recording_transport.calls == []
    assert recording_transport.token_calls == []


@pytest.mark.parametrize("name,args,path", CABLE_CALLS)
async def test_cable_change_route_sends_when_writes_enabled(name, args, path, cable_server, recording_transport):
    recording_transport.reply(httpx.Response(202, json={"started": True}, headers={"Location": "/tasks/task-1"}),
                              "POST", path)
    recording_transport.reply({"status": "COMPLETED"}, "GET", f"{path}/async-operations/task-1")
    server = cable_server(False)
    out = await server.call("invoke_tool", {"name": name, "arguments": args})
    assert "error" not in out
    assert [(call.method, call.url.path) for call in recording_transport.calls if call.method == "POST"] == [
        ("POST", path)
    ]


@pytest.mark.parametrize("product,path", CABLE_OPERATIONS)
def test_cable_cannot_bypass_read_only_by_declaring_troubleshoot(product, path):
    with pytest.raises(Refused, match="read-only"):
        Gate(read_only=True).check(product, "POST", path, {}, kind="troubleshoot")


async def test_ping_remains_available_through_read_route(cable_server, recording_transport):
    server = cable_server(True)
    out = await server.call("invoke_read_tool", {
        "name": "mist_ping_from_device", "arguments": {"site_id": "s1", "device_id": "d1"},
    })
    assert out == {"ok": True}
    assert [(call.method, call.url.path) for call in recording_transport.calls] == [
        ("POST", "/api/v1/sites/s1/devices/d1/ping")
    ]


@pytest.mark.parametrize("name", ["clearpass_set_guest_enabled", "clearpass_set_service_enabled"])
def test_clearpass_on_off_is_config(name):
    assert labels("clearpass")[name]["kind"] == "config"
    assert casper_word_kind(name) == "config"


def test_mist_nac_mac_is_config_under_a_name_without_user():
    mist = labels("mist")
    assert "mist_upsert_user_mac" not in mist
    assert mist["mist_upsert_nac_mac"]["kind"] == "config"
    assert casper_word_kind("mist_upsert_nac_mac") == "config"
