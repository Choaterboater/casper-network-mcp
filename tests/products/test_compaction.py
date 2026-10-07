"""Big Central replies are trimmed by default, with a ``full``/``full_alerts`` opt-in.

These guard the token budget: alerts, events, clients and radio/scope reads used to hand
back the raw vendor records (alert ``action``/``rootCause`` essays, duplicated ``raw``
copies, null-heavy client rows), which crowded the 16 KB a caller sees per call.

Every sample here uses placeholders only (MACs ``02:00:00:...``, TEST-NET-1 addresses).
"""

from __future__ import annotations

import pytest
from conftest import Backend

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products.central import monitoring

#: Each compacted tool and the exact way its description says to ask for the full data.
COMPACTED = {
    "central_site_overview": "full_alerts=True",
    "get_channel_utilization": "full=True",
    "find_scope": "full=True",
    "list_clients": "full=True",
    "list_events": "full=True",
}


@pytest.fixture
def central_monitoring(recording_transport, central_client_factory) -> Backend:
    from casper_network_mcp.products.central import tools as central

    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    servers = dict(central.backends(client=lambda: client))
    return Backend(servers["central-monitoring"])


_HEAVY_ALERT = {
    "id": "a1",
    "severity": "Critical",
    "name": "Device Offline",
    "summary": "Central stopped hearing from the switch.",
    "rootCause": ['{"text":"licensing / subscription issue with the switch."}'],
    "action": [
        {
            "solution": ["Verify the device status in the cloud portal.", "Contact support."],
            "rootCause": ['{"text":"licensing"}'],
        }
    ],
}


def _reply_overview(recording_transport) -> None:
    recording_transport.reply({"items": []}, "GET", "/network-monitoring/v1/sites-health")
    recording_transport.reply({"items": []}, "GET", "/network-monitoring/v1/devices")
    recording_transport.reply({"items": [_HEAVY_ALERT]}, "GET", "/network-notifications/v1/alerts")


async def test_site_overview_compacts_alerts_by_default(recording_transport, central_monitoring):
    _reply_overview(recording_transport)
    out = await central_monitoring.call("central_site_overview", {"site_id": "100"})
    top = out["top_alerts"][0]
    assert top["id"] == "a1" and top["severity"] == "Critical" and top["name"] == "Device Offline"
    assert "action" not in top and "rootCause" not in top


async def test_site_overview_full_alerts_keeps_the_essays(recording_transport, central_monitoring):
    _reply_overview(recording_transport)
    out = await central_monitoring.call("central_site_overview", {"site_id": "100", "full_alerts": True})
    assert out["top_alerts"][0]["action"]
    assert out["top_alerts"][0]["rootCause"]


_RADIOS = {
    "count": 1,
    "items": [
        {
            "band": "5 GHz",
            "channel": "108E",
            "utilization": 1,
            "noise": -99,
            "txPower": 19,
            "clientCount": 1,
            "macAddress": "02:00:00:00:00:01",
        }
    ],
}


async def test_channel_utilization_drops_the_raw_copy(recording_transport, central_monitoring):
    recording_transport.reply(_RADIOS, "GET", "/network-monitoring/v1/aps/SN1/radios")
    out = await central_monitoring.call("get_channel_utilization", {"serial_number": "SN1"})
    assert out["radios"][0]["channel"] == "108E"
    assert "raw" not in out
    full = await central_monitoring.call("get_channel_utilization", {"serial_number": "SN1", "full": True})
    assert full["raw"]


def test_find_scope_drops_the_raw_copy(monkeypatch):
    monkeypatch.setattr(
        monitoring,
        "list_scopes",
        lambda **kw: {"items": [{"scope_id": "100", "scope_name": "HQ", "scope_type": "SITE"}]},
    )
    out = monitoring.find_scope("hq")
    assert out["items"][0]["scope_name"] == "HQ"
    assert "raw" not in out["items"][0]
    full = monitoring.find_scope("hq", full=True)
    assert full["items"][0]["raw"]["scope_id"] == "100"


_CLIENT = {
    "macAddress": "02:00:00:00:00:02",
    "clientConnectionType": "Wireless",
    "wirelessBand": "5 GHz",
    "wirelessChannel": "108E",
    "connectedTo": "ap-1",
    "ipv4": "192.0.2.10",
    "clientOperatingSystem": "Unknown",
    "snr": 21,
    "radioMacAddress": "02:00:00:00:00:03",
    "mloOperMode": "MLO Disabled",
    "tunnelId": None,
}


async def test_list_clients_compacts_by_default(recording_transport, central_monitoring):
    recording_transport.reply({"items": [_CLIENT]}, "GET", "/network-monitoring/v1/clients")
    out = await central_monitoring.call("list_clients", {"site_id": "100"})
    row = out["items"][0]
    assert row["macAddress"] == "02:00:00:00:00:02" and row["connectedTo"] == "ap-1"
    assert "radioMacAddress" not in row and "mloOperMode" not in row and "tunnelId" not in row
    full = await central_monitoring.call("list_clients", {"site_id": "100", "full": True})
    assert full["items"][0]["radioMacAddress"] == "02:00:00:00:00:03"


class _FakeReads:
    def get_events(self, serial_number: str, hours: int = 24) -> list[dict[str, object]]:
        return [
            {
                "timeAt": "2026-01-01T00:00:00Z",
                "eventName": "Client Poor SNR",
                "severity": "negative",
                "description": "x" * 500,
                "eventIdentifier": "e1",
                "bssid": "02:00:00:00:00:04",
            }
        ]


async def test_list_events_compacts_by_default(monkeypatch, central_monitoring):
    monkeypatch.setattr(monitoring, "get_mcp_client", lambda: _FakeReads())
    out = await central_monitoring.call("list_events", {"serial_number": "SN1"})
    row = out["items"][0]
    assert row["eventName"] == "Client Poor SNR"
    assert len(row["description"]) == 200
    assert "eventIdentifier" not in row and "bssid" not in row
    full = await central_monitoring.call("list_events", {"serial_number": "SN1", "full": True})
    assert full["items"][0]["eventIdentifier"] == "e1"
    assert len(full["items"][0]["description"]) == 500


@pytest.mark.parametrize("name,how", sorted(COMPACTED.items()))
def test_each_compacted_tool_says_full_data_is_available(name, how, central_monitoring):
    text = central_monitoring.description(name)
    assert "raw" in text, f"{name} does not say the raw/full data is available"


@pytest.mark.parametrize("name,how", sorted(COMPACTED.items()))
def test_each_compacted_tool_says_how_to_ask(name, how, central_monitoring):
    text = central_monitoring.description(name)
    assert how in text, f"{name} does not say how to ask for the full data ({how})"
