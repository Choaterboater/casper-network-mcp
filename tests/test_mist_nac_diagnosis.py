"""Proof for the NAC diagnosis tools: ``mist_wlan_security_summary`` and ``mist_nac_troubleshoot``.

Kept in its own file so it is not part of the code change it proves: with the
tools, labels or index reverted, these tests fail; with the change they pass.
"""

from __future__ import annotations

from casper_network_mcp.core.kinds import labels
from casper_network_mcp.products.mist import tools as mist
from casper_network_mcp.router import find


def test_nac_diagnosis_tools_are_registered_reads():
    names = {t.name for t in mist.backend_tools()}
    assert {"mist_wlan_security_summary", "mist_nac_troubleshoot"} <= names
    rows = labels("mist")
    assert rows["mist_wlan_security_summary"]["kind"] == "read"
    assert rows["mist_nac_troubleshoot"]["kind"] == "read"


def test_find_tool_routes_nac_troubleshooting():
    names = [h["name"] for h in find.find_tool("why is mist nac not working allow all mac", top_k=3, product="mist")]
    assert "mist_nac_troubleshoot" in names


async def test_wlan_security_summary_flags_mpsk(recording_transport, mist_backend):
    recording_transport.reply(
        {
            "id": "w1",
            "ssid": "HealthSecurePSK",
            "auth": {"type": "psk", "enable_mac_auth": True},
            "mist_nac": {"enabled": True},
            "dynamic_psk": {"enabled": False},
            "vlan_id": 10,
        },
        "GET",
        "/api/v1/orgs/o1/wlans/w1",
    )
    recording_transport.reply(
        {"results": [{"id": "p1", "name": "IoT", "ssid": "HealthSecurePSK", "usage": "multi"}]},
        "GET",
        "/api/v1/orgs/o1/psks",
    )
    out = await mist_backend.call("mist_wlan_security_summary", {"org_id": "o1", "wlan_id": "w1"})
    assert out["cloud_psks_bound"][0]["name"] == "IoT"
    assert out["dynamic_psk_present"] is True
    assert out["mpsk_active"] is True
    assert "MPSK active" in out["verdict"]


async def test_wlan_security_summary_is_mab_when_no_cloud_psk(recording_transport, mist_backend):
    recording_transport.reply(
        {
            "id": "w1",
            "ssid": "HCA-MAB",
            "auth": {"type": "open", "enable_mac_auth": True},
            "mist_nac": {"enabled": True},
            "vlan_id": 10,
        },
        "GET",
        "/api/v1/orgs/o1/wlans/w1",
    )
    recording_transport.reply({"results": []}, "GET", "/api/v1/orgs/o1/psks")
    out = await mist_backend.call("mist_wlan_security_summary", {"org_id": "o1", "wlan_id": "w1"})
    assert out["mpsk_active"] is False
    assert "MAB" in out["verdict"]


async def test_nac_troubleshoot_names_the_mpsk_cause(recording_transport, mist_backend):
    recording_transport.reply(
        {"results": [{"mac": "aabbcc001122", "auth_type": "psk-mab", "nacrule_matched": False}]},
        "GET",
        "/api/v1/sites/s1/nac_clients/search",
    )
    recording_transport.reply(
        {
            "results": [
                {"type": "NAC_CLIENT_PPSK_KEY_NOT_FOUND", "mac": "aabbcc001122", "ssid": "HealthSecurePSK"},
                {"type": "NAC_CLIENT_PERMIT", "mac": "aabbcc001122", "nacrule_name": "Allow all"},
            ]
        },
        "GET",
        "/api/v1/sites/s1/nac_clients/events/search",
    )
    recording_transport.reply(
        {
            "items": [
                {
                    "id": "r1",
                    "name": "Home",
                    "action": "allow",
                    "enabled": True,
                    "order": 1,
                    "matching": {"auth_type": "psk"},
                }
            ]
        },
        "GET",
        "/api/v1/orgs/o1/nacrules",
    )
    out = await mist_backend.call(
        "mist_nac_troubleshoot", {"org_id": "o1", "site_id": "s1", "mac": "aa:bb:cc:00:11:22"}
    )
    assert out["event_counts"]["NAC_CLIENT_PPSK_KEY_NOT_FOUND"] == 1
    assert out["event_counts"]["NAC_CLIENT_PERMIT"] == 1
    assert "MPSK lookup intercepting" in out["verdict"]
    assert out["rules_with_unknown_auth_type"] == ["Home"]
