"""Overview tools for broad questions (Task 13): one read call gives health, device counts and top alarms.

Also ``mist_list_site_clients`` (who is on a site's wifi), from mist-mcp's
``clients_list`` (MIT). All of them only read; the spec check (tests/test_spec.py)
checks every request they make against the bundled specs.
"""

from __future__ import annotations

import pytest
from conftest import Backend

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.kinds import labels


@pytest.fixture
def clearpass_backend(recording_transport, clearpass_client_factory) -> Backend:
    from casper_network_mcp.products.clearpass import tools as clearpass

    client = clearpass_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    return Backend(clearpass.backend(client=lambda: client))


@pytest.fixture
def central_monitoring(recording_transport, central_client_factory) -> Backend:
    from casper_network_mcp.products.central import tools as central

    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    servers = dict(central.backends(client=lambda: client))
    return Backend(servers["central-monitoring"])


def test_overviews_are_reads():
    assert labels("mist")["mist_site_overview"]["kind"] == "read"
    assert labels("mist")["mist_list_site_clients"]["kind"] == "read"
    assert labels("central")["central_site_overview"]["kind"] == "read"
    assert labels("clearpass")["clearpass_overview"]["kind"] == "read"


async def test_mist_site_overview(recording_transport, mist_backend):
    recording_transport.reply(
        {"id": "s1", "name": "Branch-12", "num_ap": 2, "num_clients": 20, "secret_field": "x"},
        "GET",
        "/api/v1/sites/s1/stats",
    )
    recording_transport.reply(
        [
            {"type": "ap", "status": "connected"},
            {"type": "ap", "status": "disconnected"},
            {"type": "switch", "status": "connected"},
        ],
        "GET",
        "/api/v1/sites/s1/stats/devices",
    )
    recording_transport.reply(
        {
            "results": [
                {"id": "a1", "severity": "info", "timestamp": 30},
                {"id": "a2", "severity": "critical", "timestamp": 10},
                {"id": "a3", "severity": "warn", "timestamp": 20},
                {"id": "a4", "severity": "critical", "timestamp": 40},
            ]
        },
        "GET",
        "/api/v1/sites/s1/alarms/search",
    )
    out = await mist_backend.call("mist_site_overview", {"site_id": "s1", "top_alarms": 3})
    assert out["site"] == {"id": "s1", "name": "Branch-12", "num_ap": 2, "num_clients": 20}
    assert out["devices"] == {
        "total": 3,
        "by_type": {"ap": {"connected": 1, "disconnected": 1}, "switch": {"connected": 1}},
    }
    assert [a["id"] for a in out["top_alarms"]] == ["a4", "a2", "a3"]
    assert out["alarms_by_severity"] == {"critical": 2, "warn": 1, "info": 1}
    assert out["degraded"] is False
    assert {c.method for c in recording_transport.calls} == {"GET"}
    devices_call = next(c for c in recording_transport.calls if c.url.path.endswith("/stats/devices"))
    assert devices_call.url.params["type"] == "all"


async def test_mist_site_overview_keeps_going_when_one_part_fails(recording_transport, mist_backend):
    recording_transport.reply((500, {"detail": "boom"}), "GET", "/api/v1/sites/s1/alarms/search")
    recording_transport.reply({"id": "s1", "name": "Branch-12"}, "GET", "/api/v1/sites/s1/stats")
    recording_transport.reply([], "GET", "/api/v1/sites/s1/stats/devices")
    out = await mist_backend.call("mist_site_overview", {"site_id": "s1"})
    assert out["degraded"] is True and "error" in out["top_alarms"]
    assert out["site"]["name"] == "Branch-12"


async def test_mist_site_overview_refuses_a_bad_site_id(recording_transport, mist_backend):
    out = await mist_backend.call("mist_site_overview", {"site_id": "a/../b"})
    assert "error" in out and recording_transport.calls == []


async def test_mist_list_site_clients_filters_by_ssid(recording_transport, mist_backend):
    recording_transport.reply(
        {"results": [{"mac": "00005e005301", "ssid": "Guest", "hostname": "laptop-1"}], "next": None},
        "GET",
        "/api/v1/sites/s1/clients/search",
    )
    out = await mist_backend.call("mist_list_site_clients", {"site_id": "s1", "ssid": "Guest"})
    call = recording_transport.calls[0]
    assert call.url.params["ssid"] == "Guest" and call.url.params["duration"] == "1d"
    assert out["clients"]["items"][0]["hostname"] == "laptop-1"


async def test_central_site_overview(recording_transport, central_monitoring):
    recording_transport.reply(
        {"items": [{"siteId": "100", "siteName": "Branch-12", "health": {"good": 90}}]},
        "GET",
        "/network-monitoring/v1/sites-health",
    )
    recording_transport.reply(
        {
            "items": [
                {"deviceType": "ACCESS_POINT", "status": "ONLINE"},
                {"deviceType": "ACCESS_POINT", "status": "OFFLINE"},
                {"deviceType": "SWITCH", "status": "ONLINE"},
            ]
        },
        "GET",
        "/network-monitoring/v1/devices",
    )
    recording_transport.reply(
        {
            "items": [
                {"id": "x1", "severity": "Major"},
                {"id": "x2", "severity": "Critical"},
                {"id": "x3", "severity": "Minor"},
            ]
        },
        "GET",
        "/network-notifications/v1/alerts",
    )
    out = await central_monitoring.call("central_site_overview", {"site_id": "100", "top_alerts": 2})
    assert out["site_id"] == "100"
    assert out["health"]["siteName"] == "Branch-12"
    assert out["devices"]["total"] == 3
    assert out["devices"]["by_type"] == {"ACCESS_POINT": {"ONLINE": 1, "OFFLINE": 1}, "SWITCH": {"ONLINE": 1}}
    assert [a["id"] for a in out["top_alerts"]] == ["x2", "x1"]
    assert out["alerts_by_severity"] == {"CRITICAL": 1, "MAJOR": 1, "MINOR": 1}
    assert {c.method for c in recording_transport.calls} == {"GET"}
    alerts_call = next(c for c in recording_transport.calls if c.url.path.endswith("/alerts"))
    assert "siteId eq '100'" in alerts_call.url.params["filter"]


async def test_central_site_overview_quotes_the_site_id(recording_transport, central_monitoring):
    recording_transport.reply({"items": []})
    await central_monitoring.call("central_site_overview", {"site_id": "1' or '1'='1"})
    for call in recording_transport.calls:
        if "filter" in call.url.params:
            assert "siteId eq '1'' or ''1''=''1'" in call.url.params["filter"]


async def test_central_site_overview_needs_a_site(recording_transport, central_monitoring):
    out = await central_monitoring.call("central_site_overview", {})
    assert "error" in out and recording_transport.calls == []


async def test_clearpass_overview(recording_transport, clearpass_backend):
    recording_transport.reply({"cppm_version": "6.12.7"}, "GET", "/api/server/version")
    recording_transport.reply(
        {
            "_embedded": {
                "items": [
                    {
                        "name": "node-1",
                        "server_ip": "198.51.100.20",
                        "is_publisher": True,
                        "replication_status": "ENABLED",
                    }
                ]
            }
        },
        "GET",
        "/api/cluster/server",
    )

    def endpoints(request):
        status = request.url.params.get("filter", "")
        count = {'{"status":"Known"}': 7, '{"status":"Unknown"}': 3, '{"status":"Disabled"}': 1}.get(status, 11)
        return {"count": count, "_embedded": {"items": []}}

    recording_transport.reply(endpoints, "GET", "/api/endpoint")
    recording_transport.reply(
        {
            "count": 4,
            "_embedded": {
                "items": [{"id": "1", "username": "visitor1", "auth_status": "FAILED", "reason": "bad password"}]
            },
        },
        "GET",
        "/api/session",
    )
    out = await clearpass_backend.call("clearpass_overview", {"top_failures": 5})
    assert out["version"] == {"cppm_version": "6.12.7"}
    assert out["cluster"]["total"] == 1 and out["cluster"]["servers"][0]["name"] == "node-1"
    assert out["endpoints"] == {"total": 11, "Known": 7, "Unknown": 3, "Disabled": 1}
    assert out["auth_failures"]["total"] == 4
    assert out["auth_failures"]["recent"][0]["reason"] == "bad password"
    assert out["degraded"] is False
    assert {c.method for c in recording_transport.calls} == {"GET"}
