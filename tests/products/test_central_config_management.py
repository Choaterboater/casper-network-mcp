"""get_device_config_management: which system manages a device's configuration, in one read.

Sample answers only (field names as Central's device-inventory and config-health
reads return them); no live calls.
"""

from __future__ import annotations

import pytest

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.annotations import READ_ONLY
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.kinds import labels
from casper_network_mcp.products.central import tools as central

INVENTORY = "/network-monitoring/v1/device-inventory"
CONFIG_HEALTH = "/network-config/v1alpha1/config-health/devices"
TOOL = "get_device_config_management"


def _inventory_record(serial: str, **extra):
    record = {
        "serialNumber": serial,
        "macAddress": "02:00:00:00:00:01",
        "deviceName": "Example-AP",
        "deviceType": "ACCESS_POINT",
        "deviceFunction": "Campus Access Point",
        "isProvisioned": "Yes",
        "siteId": "100000000000000001",
        "siteName": "Example Site",
        "deviceGroupId": None,
        "deviceGroupName": None,
        "ipv4": "192.0.2.10",
        "status": "ONLINE",
    }
    record.update(extra)
    return record


def _config_health(tracked: list[dict], fleet_total: int):
    """Config health: the per-serial filter answers ``tracked``; the unfiltered count answers ``fleet_total``."""

    def answer(request):
        if "filter" in request.url.params:
            return {"devices": tracked, "count": len(tracked), "offset": 0, "total": len(tracked)}
        return {"devices": [], "count": 0, "offset": 0, "total": fleet_total}

    return answer


@pytest.fixture
def call(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    servers = dict(central.backends(client=lambda: client))
    server = servers["central-monitoring"]

    async def run(serial):
        return await sdk_compat.call_tool_raw(server, TOOL, {"serial_number": serial})

    run.server = server
    run.servers = servers
    return run


async def test_inventory_labels_without_a_device_group_or_config_health_are_not_new_central(call, recording_transport):
    recording_transport.reply({"items": [_inventory_record("SAMPLE0001")], "count": 1}, "GET", INVENTORY)
    recording_transport.reply(_config_health([], fleet_total=0), "GET", CONFIG_HEALTH)

    out = await call("SAMPLE0001")

    assert next(iter(out)) == "managed_by"
    assert out["managed_by"].startswith("Not managed by new Central")
    assert out["managed_by_new_central"] is False
    said = out["managed_by"]
    assert "profiles, device function and site assignment do not configure" in said
    assert "no new Central fix" in said
    # This server's Central login is a GreenLake one, which Classic Central does not accept.
    assert out["classic_central_reachable"] is False
    assert "can't see where" in out["next_step"]
    assert "Classic Central or the device itself" in out["next_step"]

    evidence = out["evidence"]
    assert evidence["device_group"] is None
    assert evidence["config_health"] == {
        "tracked": False,
        "config_status": None,
        "devices_tracked_in_account": 0,
    }
    inventory_labels = evidence["inventory_labels"]
    assert inventory_labels["provisioned"] == "Yes"
    assert inventory_labels["device_function"] == "Campus Access Point"
    assert inventory_labels["site"] == "Example Site"
    assert "not proof" in inventory_labels["note"]
    assert {c.method for c in recording_transport.calls} == {"GET"}


async def test_a_device_in_a_group_that_config_health_tracks_is_new_central(call, recording_transport):
    record = _inventory_record("SAMPLE0002", deviceGroupId="200000000000000002", deviceGroupName="Example Group")
    recording_transport.reply({"items": [record], "count": 1}, "GET", INVENTORY)
    tracked = [{"serial": "SAMPLE0002", "configStatus": "SYNCHRONIZED", "deviceGroupName": "Example Group"}]
    recording_transport.reply(_config_health(tracked, fleet_total=5), "GET", CONFIG_HEALTH)

    out = await call("SAMPLE0002")

    assert out["managed_by_new_central"] is True
    assert out["managed_by"].startswith("New Central manages")
    assert "Example Group" in out["managed_by"]
    assert out["evidence"]["device_group"] == {"name": "Example Group", "scope_id": "200000000000000002"}
    assert out["evidence"]["config_health"]["tracked"] is True
    assert out["evidence"]["config_health"]["config_status"] == "SYNCHRONIZED"
    assert "classic_central_reachable" not in out
    serial_filter = next(c for c in recording_transport.calls if c.url.path == CONFIG_HEALTH)
    assert serial_filter.url.params["filter"] == "serial eq 'SAMPLE0002'"


async def test_a_failed_read_is_unknown_never_a_guess(call, recording_transport):
    recording_transport.reply({"items": [_inventory_record("SAMPLE0003")], "count": 1}, "GET", INVENTORY)
    recording_transport.reply(500, "GET", CONFIG_HEALTH)

    out = await call("SAMPLE0003")

    assert out["managed_by_new_central"] is None
    assert out["managed_by"].startswith("Unknown")
    assert out["errors"]


async def test_a_device_missing_from_inventory_and_config_health_is_unknown(call, recording_transport):
    recording_transport.reply({"items": [], "count": 0}, "GET", INVENTORY)
    recording_transport.reply(_config_health([], fleet_total=3), "GET", CONFIG_HEALTH)

    out = await call("SAMPLE0004")

    assert out["managed_by_new_central"] is None
    assert "not in this login's inventory" in out["managed_by"]


def test_it_is_a_read_and_says_to_call_it_first(call):
    tool = sdk_compat.get_tool(call.server, TOOL)
    assert labels("central")[TOOL]["kind"] == "read"
    assert tool.annotations == READ_ONLY
    text = " ".join((tool.description or "").split())
    assert "before saying anything about a device's new Central config" in text
    assert "inventory labels" in text.lower()


@pytest.mark.parametrize("name", ["list_inventory", "list_devices", "find_device", "list_scope_devices"])
def test_inventory_tools_say_their_labels_are_not_config_management(call, name):
    server = next(s for s in call.servers.values() if sdk_compat.get_tool(s, name))
    text = " ".join((sdk_compat.get_tool(server, name).description or "").split())
    assert "not proof new Central manages" in text and TOOL in text, name


async def test_with_classic_central_in_reach_it_says_to_look_there_next(call, recording_transport, monkeypatch):
    from casper_network_mcp.products.central import monitoring

    monkeypatch.setattr(monitoring, "CLASSIC_CENTRAL_REACHABLE", True)
    recording_transport.reply({"items": [_inventory_record("SAMPLE0005")], "count": 1}, "GET", INVENTORY)
    recording_transport.reply(_config_health([], fleet_total=0), "GET", CONFIG_HEALTH)

    out = await call("SAMPLE0005")

    assert out["managed_by"].startswith("Not managed by new Central")
    assert out["classic_central_reachable"] is True
    assert out["next_step"].startswith("Look in Classic Central next")
