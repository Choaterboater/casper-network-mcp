"""Mist hand-written tools: spec-correct paths, a dry-run preview, no confirm step.

Behaviour ported from hpe-networking-mcp ``tests/unit/test_mist_backend.py`` and
``test_mist_nac_marvis_wan.py``, and mist-mcp ``tests/test_tools.py`` (MIT),
rewritten for the gated client and the recording fake.
"""

from __future__ import annotations

import inspect

import pytest
from conftest import body_of

from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products.mist import tools as mist


def _params(request) -> dict[str, str]:
    return dict(request.url.params)


def test_no_confirm_argument_anywhere():
    for t in mist.backend_tools():
        assert "confirm" not in inspect.signature(t.fn).parameters, t.name


def test_every_write_has_an_opt_in_preview():
    from casper_network_mcp.core.kinds import labels

    rows = labels("mist")
    for t in mist.backend_tools():
        params = inspect.signature(t.fn).parameters
        if rows[t.name]["kind"] == "read":
            assert "dry_run" not in params, t.name
        else:
            assert "dry_run" in params and params["dry_run"].default is False, t.name


def test_no_generic_write_passthrough_and_no_status_tool():
    names = {t.name for t in mist.backend_tools()}
    assert "mist_write" not in names
    assert "mist_status" not in names
    assert {"mist_get", "mist_list_sites", "mist_update_wlan", "sle_summary", "marvis_troubleshoot"} <= names


async def test_wlan_update_dry_run_sends_nothing(recording_transport, mist_backend):
    out = await mist_backend.call(
        "mist_update_wlan", {"site_id": "s1", "wlan_id": "w1", "changes": {"vlan_id": 30}, "dry_run": True}
    )
    assert out["would_send"]["method"] == "PUT" and recording_transport.calls == []
    assert out["would_send"]["path"] == "/api/v1/sites/s1/wlans/w1"
    assert out["would_send"]["body"] == {"vlan_id": 30}


async def test_wlan_update_sends_the_changes(recording_transport, mist_backend):
    recording_transport.reply({"id": "w1", "vlan_id": 30}, "PUT", "/api/v1/sites/s1/wlans/w1")
    out = await mist_backend.call("mist_update_wlan", {"site_id": "s1", "wlan_id": "w1", "changes": {"vlan_id": 30}})
    assert out["wlan"]["vlan_id"] == 30
    assert body_of(recording_transport.calls[0]) == {"vlan_id": 30}


async def test_sle_scope_enum_matches_spec(mist_backend):
    schema = mist_backend.schema("sle_summary")
    assert schema["properties"]["scope"]["enum"] == ["ap", "client", "gateway", "site", "switch"]


async def test_site_sle_summary_scope_is_only_the_spec_scopes(mist_backend):
    schema = mist_backend.schema("mist_get_site_sle_metric_summary")
    assert schema["properties"]["scope"]["enum"] == ["ap", "client", "gateway", "site", "switch"]
    doc = mist_backend.description("mist_get_site_sle_metric_summary")
    for gone in ("band", "device-os", "device-type", "wlan"):
        assert gone not in doc


async def test_path_piece_with_a_slash_is_refused_before_sending(recording_transport, mist_backend):
    out = await mist_backend.call(
        "mist_update_wlan", {"site_id": "abc/../../orgs/X", "wlan_id": "w1", "changes": {"vlan_id": 30}}
    )
    assert "slash" in out["error"] and recording_transport.calls == []


async def test_read_only_pin_refuses_a_write(recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    backend = mist.backend(client=lambda: client)
    from casper_network_mcp import sdk_compat

    out = await sdk_compat.call_tool_raw(backend, "mist_delete_wlan", {"site_id": "s1", "wlan_id": "w1"})
    assert "read-only" in out["error"] and recording_transport.calls == []


async def test_missing_login_is_structured(recording_transport):
    from casper_network_mcp import sdk_compat
    from casper_network_mcp.products.mist.client import MistClient

    client = MistClient(gate=Gate(False), token=None, transport=recording_transport)
    backend = mist.backend(client=lambda: client)
    out = await sdk_compat.call_tool_raw(backend, "mist_list_sites", {"org_id": "o1"})
    assert out == {"error": "login_missing", "product": "mist"}


# ── mist_get (read passthrough) ─────────────────────────────────────────────


async def test_mist_get_rejects_paths_outside_the_api(recording_transport, mist_backend):
    for bad in ("/admin", "/api/v1/%2e%2e/admin", "/api/v1/%252e%252e/admin"):
        out = await mist_backend.call("mist_get", {"path": bad})
        assert "error" in out, bad
    assert recording_transport.calls == []


async def test_mist_get_refuses_a_get_that_changes_something(recording_transport, mist_backend):
    out = await mist_backend.call("mist_get", {"path": "/api/v1/installer/sites/Branch-12/optimize"})
    assert "changes" in out["error"] and recording_transport.calls == []


async def test_mist_get_bounds_lists(recording_transport, mist_backend):
    recording_transport.reply([{"id": i} for i in range(30)], "GET", "/api/v1/self/apitokens")
    out = await mist_backend.call("mist_get", {"path": "/api/v1/self/apitokens", "limit": 5})
    assert len(out["items"]) == 5 and out["_pagination"]["truncated"] is True


# ── curated reads ───────────────────────────────────────────────────────────


async def test_list_sites_compacts_and_pages(recording_transport, mist_backend):
    recording_transport.reply(
        [{"id": "s1", "name": "Branch-12", "timezone": "UTC", "secret_thing": "x", "address": ""}],
        "GET",
        "/api/v1/orgs/o1/sites",
    )
    out = await mist_backend.call("mist_list_sites", {"org_id": "o1", "limit": 10, "page": 2})
    assert out["sites"]["items"] == [{"id": "s1", "name": "Branch-12", "timezone": "UTC"}]
    assert out["sites"]["server_page"] == 2
    assert _params(recording_transport.calls[0]) == {"limit": "10", "page": "2"}


async def test_get_client_normalises_the_mac(recording_transport, mist_backend):
    recording_transport.reply(
        {"mac": "aabbcc001122", "rssi": -60, "other": 1}, "GET", "/api/v1/sites/s1/stats/clients/aabbcc001122"
    )
    out = await mist_backend.call("mist_get_client", {"site_id": "s1", "mac_address": "AA:BB:CC:00:11:22"})
    assert out["normalized_mac"] == "aabbcc001122"
    assert out["client"] == {"mac": "aabbcc001122", "rssi": -60}


async def test_get_client_refuses_a_bad_mac(recording_transport, mist_backend):
    out = await mist_backend.call("mist_get_client", {"site_id": "s1", "mac_address": "nope"})
    assert "12 hex" in out["error"] and recording_transport.calls == []


async def test_list_wlans_hides_the_psk(recording_transport, mist_backend):
    recording_transport.reply(
        [{"id": "w1", "ssid": "Guest", "auth": {"type": "psk", "psk": "s3cret"}, "vlan_id": 30}],
        "GET",
        "/api/v1/sites/s1/wlans",
    )
    out = await mist_backend.call("mist_list_wlans", {"site_id": "s1"})
    wlan = out["wlans"]["items"][0]
    assert wlan["auth"] == {"type": "psk", "psk": "[hidden]"}


async def test_list_alarms_strips_empty_params(recording_transport, mist_backend):
    recording_transport.reply(
        {"results": [{"id": "a1", "type": "ap_down", "noise": 1}]}, "GET", "/api/v1/sites/s1/alarms/search"
    )
    out = await mist_backend.call("mist_list_alarms", {"site_id": "s1", "severity": "critical"})
    assert out["alarms"]["items"] == [{"id": "a1", "type": "ap_down"}]
    assert _params(recording_transport.calls[0]) == {
        "severity": "critical",
        "limit": "100",
        "duration": "1d",
        "sort": "-timestamp",
    }


async def test_nac_idps_come_from_org_setting(recording_transport, mist_backend):
    recording_transport.reply(
        {"mist_nac": {"idps": [{"id": "i1", "user_realms": ["example.com"], "x": 1}]}}, "GET", "/api/v1/orgs/o1/setting"
    )
    out = await mist_backend.call("mist_list_nac_idps", {"org_id": "o1"})
    assert out["nac_idps"]["items"] == [{"id": "i1", "user_realms": ["example.com"]}]


async def test_inventory_list_leaves_out_the_claim_code(recording_transport, mist_backend):
    recording_transport.reply(
        [{"mac": "aabbcc001122", "magic": "ABCDEFGH", "serial": "S1"}], "GET", "/api/v1/orgs/o1/inventory"
    )
    out = await mist_backend.call("mist_list_org_inventory", {"org_id": "o1", "unassigned": True})
    assert out["inventory"]["items"] == [{"mac": "aabbcc001122", "serial": "S1"}]
    assert _params(recording_transport.calls[0])["unassigned"] == "true"


async def test_switches_and_gateways_use_unified_device_stats(recording_transport, mist_backend):
    await mist_backend.call("mist_list_switches", {"site_id": "s1"})
    await mist_backend.call("mist_list_gateways", {"site_id": "s1", "status": "connected"})
    paths = [c.url.path for c in recording_transport.calls]
    assert paths == ["/api/v1/sites/s1/stats/devices"] * 2
    assert _params(recording_transport.calls[0])["type"] == "switch"
    assert _params(recording_transport.calls[1])["type"] == "gateway"


async def test_switch_ports_use_port_search(recording_transport, mist_backend):
    await mist_backend.call("mist_list_switch_ports", {"site_id": "s1", "switch_mac": "AA-BB-CC-00-11-22"})
    call = recording_transport.calls[0]
    assert call.url.path == "/api/v1/sites/s1/stats/ports/search"
    assert _params(call) == {"mac": "aabbcc001122", "device_type": "switch", "limit": "100"}


async def test_site_snapshot_reports_a_degraded_section(recording_transport, mist_backend):
    recording_transport.reply((500, {"detail": "boom"}), "GET", "/api/v1/sites/s1/alarms/search")
    out = await mist_backend.call("mist_get_site_assurance_snapshot", {"site_id": "s1"})
    assert set(out["sections"]) == {"switches", "gateways", "alarms"}
    assert "error" in out["sections"]["alarms"] and out["degraded"] is True


# ── curated writes ──────────────────────────────────────────────────────────


async def test_ack_alarm_preview_then_send(recording_transport, mist_backend):
    out = await mist_backend.call(
        "mist_ack_alarm", {"site_id": "s1", "alarm_id": "a1", "note": "seen", "dry_run": True}
    )
    assert out["would_send"] == {"method": "POST", "path": "/api/v1/sites/s1/alarms/a1/ack", "body": {"note": "seen"}}
    assert recording_transport.calls == []
    await mist_backend.call("mist_ack_alarm", {"site_id": "s1", "alarm_id": "a1"})
    assert recording_transport.calls[0].method == "POST"


async def test_user_mac_normalises_and_previews(recording_transport, mist_backend):
    out = await mist_backend.call(
        "mist_upsert_user_mac", {"org_id": "o1", "mac_address": "AA:BB:CC:00:11:22", "vlan": "30", "dry_run": True}
    )
    assert out["would_send"]["body"] == {"mac": "aabbcc001122", "vlan": "30"}
    assert recording_transport.calls == []


async def test_claim_masks_codes_in_the_preview_and_sends_a_bare_list(recording_transport, mist_backend):
    out = await mist_backend.call("mist_claim_devices", {"org_id": "o1", "claim_codes": ["ABCDEFGH"], "dry_run": True})
    assert out["would_send"]["body"] == ["...EFGH"]
    await mist_backend.call("mist_claim_devices", {"org_id": "o1", "claim_codes": ["ABCDEFGH", "IJKLMNOP"]})
    assert body_of(recording_transport.calls[0]) == ["ABCDEFGH", "IJKLMNOP"]
    out = await mist_backend.call("mist_claim_devices", {"org_id": "o1", "claim_codes": []})
    assert "at least one" in out["error"]


async def test_marvis_settings_are_wrapped(recording_transport, mist_backend):
    await mist_backend.call("mist_set_marvis_settings", {"org_id": "o1", "settings": {"self_driving": {}}})
    assert body_of(recording_transport.calls[0]) == {"marvis": {"self_driving": {}}}


# ── from mist-mcp ───────────────────────────────────────────────────────────


async def test_sle_metrics_site_scope_uses_site_id_twice(recording_transport, mist_backend):
    await mist_backend.call("sle_metrics", {"site_id": "s1"})
    assert recording_transport.calls[0].url.path == "/api/v1/sites/s1/sle/site/s1/metrics"


async def test_sle_summary_for_an_ap(recording_transport, mist_backend):
    await mist_backend.call(
        "sle_summary", {"site_id": "s1", "metric": "coverage", "scope": "ap", "scope_id": "dev-9", "duration": "7d"}
    )
    call = recording_transport.calls[0]
    assert call.url.path == "/api/v1/sites/s1/sle/ap/dev-9/metric/coverage/summary"
    assert _params(call) == {"duration": "7d"}


async def test_sle_summary_needs_a_scope_id_for_a_device(recording_transport, mist_backend):
    out = await mist_backend.call("sle_summary", {"site_id": "s1", "metric": "coverage", "scope": "ap"})
    assert "scope_id" in out["error"] and recording_transport.calls == []


async def test_sle_org_summary(recording_transport, mist_backend):
    await mist_backend.call("sle_org_summary", {"org_id": "o1", "sle": "wifi"})
    call = recording_transport.calls[0]
    assert call.url.path == "/api/v1/orgs/o1/insights/sites-sle"
    assert _params(call) == {"duration": "1d", "sle": "wifi"}


async def test_marvis_troubleshoot_client_or_site(recording_transport, mist_backend):
    await mist_backend.call("marvis_troubleshoot", {"org_id": "o1", "mac": "AA:BB:CC:00:11:22"})
    assert _params(recording_transport.calls[-1]) == {"mac": "aabbcc001122"}
    await mist_backend.call(
        "marvis_troubleshoot", {"org_id": "o1", "site_id": "s1", "network": "wired", "start": "-1d"}
    )
    assert _params(recording_transport.calls[-1]) == {"site_id": "s1", "type": "wired", "start": "-1d"}
    out = await mist_backend.call("marvis_troubleshoot", {"org_id": "o1"})
    assert "mac" in out["error"]


async def test_org_alarm_search_and_lifecycle(recording_transport, mist_backend):
    await mist_backend.call("alarms_list", {"org_id": "o1", "acked": False, "severity": "major", "limit": 10})
    assert _params(recording_transport.calls[-1]) == {
        "acked": "false",
        "severity": "major",
        "limit": "10",
        "duration": "1d",
    }
    await mist_backend.call("alarms_ack", {"org_id": "o1", "alarm_ids": ["a1"]})
    assert body_of(recording_transport.calls[-1]) == {"alarm_ids": ["a1"]}
    await mist_backend.call("alarms_unack", {"org_id": "o1", "alarm_ids": ["a1"], "note": "still flapping"})
    assert body_of(recording_transport.calls[-1]) == {"alarm_ids": ["a1"], "note": "still flapping"}
    out = await mist_backend.call("alarms_ack", {"org_id": "o1", "alarm_ids": []})
    assert "alarm_ids" in out["error"]


async def test_events_by_kind(recording_transport, mist_backend):
    await mist_backend.call("site_events", {"site_id": "s1", "kind": "devices", "mac": "aabbcc001122"})
    await mist_backend.call("org_events", {"org_id": "o1", "kind": "clients", "search_after": "x9"})
    first, second = recording_transport.calls
    assert first.url.path == "/api/v1/sites/s1/devices/events/search"
    assert _params(first)["mac"] == "aabbcc001122"
    assert second.url.path == "/api/v1/orgs/o1/clients/events/search"
    assert _params(second)["search_after"] == "x9"


async def test_client_events_have_no_mac_filter_in_the_spec(recording_transport, mist_backend):
    out = await mist_backend.call("site_events", {"site_id": "s1", "kind": "clients", "mac": "aabbcc001122"})
    assert "mac" in out["error"] and recording_transport.calls == []


async def test_audit_logs_and_rogues(recording_transport, mist_backend):
    await mist_backend.call("audit_logs", {"org_id": "o1", "admin_name": "Example Admin"})
    await mist_backend.call("rogue_aps", {"site_id": "s1", "rogue_type": "spoof"})
    assert recording_transport.calls[0].url.path == "/api/v1/orgs/o1/logs"
    assert recording_transport.calls[1].url.path == "/api/v1/sites/s1/insights/rogues"
    assert _params(recording_transport.calls[1])["type"] == "spoof"


async def test_inventory_assign_unassign_delete(recording_transport, mist_backend):
    await mist_backend.call("inventory_assign", {"org_id": "o1", "macs": ["AA:BB:CC:00:11:22"], "site_id": "s2"})
    assert body_of(recording_transport.calls[-1]) == {"op": "assign", "macs": ["aabbcc001122"], "site_id": "s2"}
    await mist_backend.call("inventory_unassign", {"org_id": "o1", "macs": ["aabbcc001122"]})
    assert body_of(recording_transport.calls[-1]) == {"op": "unassign", "macs": ["aabbcc001122"]}
    out = await mist_backend.call("inventory_delete", {"org_id": "o1", "macs": ["aabbcc001122"], "dry_run": True})
    assert out["would_send"] == {
        "method": "PUT",
        "path": "/api/v1/orgs/o1/inventory",
        "body": {"op": "delete", "macs": ["aabbcc001122"]},
    }
    assert [c.method for c in recording_transport.calls] == ["PUT", "PUT"]


def test_inventory_kinds_are_honest():
    from casper_network_mcp.core.kinds import labels

    rows = labels("mist")
    assert rows["inventory_delete"]["kind"] == "delete"
    assert rows["mist_delete_wlan"]["kind"] == "delete"
    assert rows["inventory_assign"]["kind"] == "config"
    assert rows["mist_get"]["kind"] == "read"


async def test_api_error_comes_back_plainly(recording_transport, mist_backend):
    recording_transport.reply((401, {"detail": "Invalid token."}), "GET", "/api/v1/orgs/o1/sites")
    out = await mist_backend.call("mist_list_sites", {"org_id": "o1"})
    assert out == {"error": "login_expired", "product": "mist"}


async def test_other_api_errors_keep_status_and_detail(recording_transport, mist_backend):
    recording_transport.reply((403, {"detail": "Forbidden."}), "GET", "/api/v1/orgs/o1/sites")
    out = await mist_backend.call("mist_list_sites", {"org_id": "o1"})
    assert out["status"] == 403 and out["detail"] == "Forbidden." and "login" not in out


@pytest.mark.parametrize("name", ["mist_list_sites", "sle_org_summary", "alarms_list"])
def test_org_id_is_required(name, mist_backend):
    assert "org_id" in mist_backend.schema(name)["required"]
