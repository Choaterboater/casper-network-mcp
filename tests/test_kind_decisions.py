"""The owner's kind decisions: a cable test is troubleshooting, on/off toggles and NAC MACs are config.

* A cable (TDR) test on Central CX/AOS-S and Mist runs like ping or show: it is
  on the troubleshooting list, ``invoke_read_tool`` and ``--read-only`` run
  it, and its reason still says it briefly takes the tested port's link down.
* ClearPass guest and service on/off are ordinary config changes.
* Mist's known-client MAC entry is an ordinary config change, under a name
  Casper does not read as an account change (no "user").
"""

from __future__ import annotations

import pytest
from conftest import Backend
from test_labels import casper_word_kind

from casper_network_mcp import sdk_compat
from casper_network_mcp.core import kinds
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.kinds import TROUBLESHOOT_OPS, TROUBLESHOOT_REASONS, kind_for_operation, labels
from casper_network_mcp.products.central import compat
from casper_network_mcp.products.central import tools as central
from casper_network_mcp.router import dispatch

CABLE_TESTS = [
    ("POST", "/network-troubleshooting/v1/cx/{serial-number}/cableTest"),
    ("POST", "/network-troubleshooting/v1/aos-s/{serial-number}/cableTest"),
    ("POST", "/api/v1/sites/{site_id}/devices/{device_id}/cable_test"),
]
CABLE_TOOLS = [
    "cable_test",
    "central_initiate_cx_cable_test_v1",
    "central_initiate_pvos_cable_test_v1",
    "mist_cable_test_from_switch",
]


@pytest.mark.parametrize("op", CABLE_TESTS)
def test_cable_test_is_troubleshooting_with_an_honest_reason(op):
    assert op in TROUBLESHOOT_OPS
    assert op not in kinds.KIND_OVERRIDES
    assert kind_for_operation(*op) == "troubleshoot"
    assert "briefly takes the tested port's link down" in TROUBLESHOOT_REASONS[op]
    assert "changes nothing" not in TROUBLESHOOT_REASONS[op]


def test_central_cable_test_row_is_troubleshoot():
    row = labels("central")["cable_test"]
    assert row["kind"] == "troubleshoot"
    assert "briefly takes the tested port's link down" in row["checked"]


@pytest.mark.parametrize("name", CABLE_TOOLS)
def test_cable_tests_run_through_invoke_read_tool(name):
    from casper_network_mcp.router.index import catalog

    entry = catalog().get(name)
    assert entry is not None, name
    assert entry.kind == "troubleshoot", name
    assert dispatch.is_read_tool(entry), name
    assert casper_word_kind(name) == "troubleshoot", name


@pytest.mark.parametrize(
    "path",
    [
        "/network-troubleshooting/v1/cx/SG12345678/cableTest",
        "/network-troubleshooting/v1/aos-s/SG12345678/cableTest",
        "/api/v1/sites/s1/devices/d1/cable_test",
    ],
)
def test_read_only_pin_lets_a_cable_test_through(path):
    Gate(read_only=True).check("central", "POST", path, {}, kind="troubleshoot")


async def test_central_cable_test_tool_runs_under_read_only(recording_transport, central_client_factory, monkeypatch):
    monkeypatch.setattr(compat, "POLL_INTERVAL", 0)
    monkeypatch.setattr(compat, "POLL_MAX", 1)
    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    servers = dict(central.backends(client=lambda: client))
    server = next(s for s in servers.values() if sdk_compat.get_tool(s, "cable_test") is not None)
    out = await Backend(server).call(
        "cable_test", {"serial_number": "SG12345678", "ports": ["1/1/1"], "device_type": "CX"}
    )
    assert "read-only" not in str(out)
    posts = [c for c in recording_transport.calls if c.method == "POST"]
    assert posts and posts[0].url.path.endswith("/cableTest")


@pytest.mark.parametrize("name", ["clearpass_set_guest_enabled", "clearpass_set_service_enabled"])
def test_clearpass_on_off_is_config(name):
    assert labels("clearpass")[name]["kind"] == "config"
    assert casper_word_kind(name) == "config"


def test_mist_nac_mac_is_config_under_a_name_without_user():
    mist = labels("mist")
    assert "mist_upsert_user_mac" not in mist
    assert mist["mist_upsert_nac_mac"]["kind"] == "config"
    assert casper_word_kind("mist_upsert_nac_mac") == "config"
