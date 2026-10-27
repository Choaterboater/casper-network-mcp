"""Sample data, not from a real network. GET-only compact AirPrint diagnostics."""

from __future__ import annotations

import json

import pytest

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products.mist import tools
from casper_network_mcp.router.find import find_tool

TOOL = "mist_wlan_discovery_summary"
DERIVED = "/api/v1/sites/s1/wlans/derived"
SITES = "/api/v1/orgs/o1/sites"
WLAN = {
    "id": "w1",
    "ssid": "Example",
    "enabled": True,
    "template_id": "t1",
    "for_site": False,
    "vlan_id": 200,
    "isolation": False,
    "l2_isolation": False,
    "limit_bcast": True,
    "allow_mdns": False,
    "allow_ssdp": False,
    "bonjour": {"services": {}, "additional_vlan_ids": ""},
    "auth": {"type": "psk", "psk": "synthetic-do-not-return"},
    "portal": {"notes": "unnecessary " * 2000},
}


def seed_scope(transport):
    transport.reply(
        {"name": "sample", "privileges": [{"scope": "org", "role": "observer", "org_id": "o1", "name": "Example Org"}]},
        "GET",
        "/api/v1/self",
    )
    transport.reply([{"id": "s1", "name": "Example Site"}, {"id": "s2", "name": "Empty"}], "GET", SITES)
    transport.reply([WLAN], "GET", DERIVED)
    transport.reply([], "GET", "/api/v1/sites/s2/wlans/derived")


async def test_one_tool_resolves_ssid_and_returns_compact_evidence(recording_transport, mist_backend):
    seed_scope(recording_transport)
    out = await mist_backend.call(TOOL, {"ssid": "Example"})
    assert out["status"] == "matched" and out["sites_checked"] == 2
    (match,) = out["matches"]
    assert match["site_id"] == "s1" and match["id"] == "w1"
    assert match["template_id"] == "t1" and match["for_site"] is False
    assert match["settings"]["allow_mdns"] is False
    assert match["settings"]["isolation"] is False
    assert match["findings"] == ["mdns_filtered"]
    encoded = json.dumps(out)
    assert len(encoded.encode()) < 2500
    assert "synthetic-do-not-return" not in encoded and "auth" not in encoded and "portal" not in encoded
    assert len(recording_transport.calls) == 4  # self + sites + two derived WLAN reads, no inventory
    assert all(c.method == "GET" for c in recording_transport.calls)
    derived = [c for c in recording_transport.calls if c.url.path.endswith("/derived")]
    assert all(dict(c.url.params) == {"resolve": "true"} for c in derived)


async def test_find_then_diagnose_in_two_mcp_calls(recording_transport, server_with_replies):
    from mcp.client import Client

    seed_scope(recording_transport)
    async with Client(server_with_replies({}, read_only=True)) as client:
        found = await client.call_tool(
            "find_tool", {"query": "AirPrint discovery", "product": "mist", "top_k": 1, "include_schema": True}
        )
        (hit,) = found.structured_content["result"]
        assert hit["name"] == TOOL and hit["kind"] == "read"
        result = await client.call_tool("invoke_read_tool", {"name": hit["name"], "arguments": {"ssid": "Example"}})
    out = json.loads(result.content[0].text)
    assert out["status"] == "matched"
    assert len(recording_transport.calls) == 4
    assert len(json.dumps(found.structured_content).encode()) + len(json.dumps(out).encode()) < 4000


async def test_known_site_skips_scope_reads_and_uses_derived_wlans(recording_transport, mist_backend):
    recording_transport.reply([WLAN], "GET", DERIVED)
    out = await mist_backend.call(TOOL, {"ssid": "Example", "site_id": "s1"})
    assert out["status"] == "matched"
    assert [c.url.path for c in recording_transport.calls] == [DERIVED]


async def test_known_org_skips_self(recording_transport, mist_backend):
    seed_scope(recording_transport)
    out = await mist_backend.call(TOOL, {"ssid": "Example", "org_id": "o1"})
    assert out["status"] == "matched"
    assert len(recording_transport.calls) == 3
    assert all(c.url.path != "/api/v1/self" for c in recording_transport.calls)


async def test_site_only_observer_never_needs_org_permission(recording_transport, mist_backend):
    recording_transport.reply(
        {"privileges": [{"scope": "site", "role": "observer", "site_id": "s1", "org_id": "o1", "name": "Example"}]},
        "GET",
        "/api/v1/self",
    )
    recording_transport.reply([WLAN], "GET", DERIVED)
    out = await mist_backend.call(TOOL, {"ssid": "Example"})
    assert out["status"] == "matched"
    assert [c.url.path for c in recording_transport.calls] == ["/api/v1/self", DERIVED]


async def test_multiple_orgs_require_explicit_scope(recording_transport, mist_backend):
    recording_transport.reply(
        {"privileges": [{"scope": "org", "org_id": org, "role": "observer"} for org in ("o1", "o2")]},
        "GET",
        "/api/v1/self",
    )
    out = await mist_backend.call(TOOL, {"ssid": "Example"})
    assert out["status"] == "scope_required" and len(out["orgs"]) == 2
    assert len(recording_transport.calls) == 1


async def test_duplicate_ssids_are_not_silently_selected(recording_transport, mist_backend):
    seed_scope(recording_transport)
    recording_transport.reply([WLAN], "GET", "/api/v1/sites/s2/wlans/derived")
    out = await mist_backend.call(TOOL, {"ssid": "Example"})
    assert out["status"] == "multiple_matches"
    assert {match["site_id"] for match in out["matches"]} == {"s1", "s2"}


async def test_large_org_does_not_launch_an_unbounded_scan(recording_transport, mist_backend):
    recording_transport.reply([{"id": f"s{i}"} for i in range(11)], "GET", SITES)
    out = await mist_backend.call(TOOL, {"ssid": "Example", "org_id": "o1"})
    assert out["status"] == "scope_required" and len(out["sites"]) == 10
    assert len(recording_transport.calls) == 1
    assert dict(recording_transport.calls[0].url.params) == {"limit": "11", "page": "1"}


@pytest.mark.parametrize(
    "reply",
    [
        {"items": [], "_pagination": {"truncated": True, "total": 12}},
        {"items": [], "_pagination": {"total": 12}},
        {"text": "cut short", "truncated": True},
        {"items": [], "_response_bounds": {"truncated": True}},
        {"ok": True},
        [None],
    ],
)
async def test_incomplete_wlan_data_is_never_not_found(reply, recording_transport, mist_backend):
    recording_transport.reply(reply, "GET", DERIVED)
    out = await mist_backend.call(TOOL, {"ssid": "Example", "site_id": "s1"})
    assert out["status"] == "incomplete" and out["failures"]


async def test_incomplete_sites_do_not_start_a_scan(recording_transport, mist_backend):
    recording_transport.reply({"items": [{"id": "s1"}], "_pagination": {"truncated": True}}, "GET", SITES)
    out = await mist_backend.call(TOOL, {"ssid": "Example", "org_id": "o1"})
    assert out["status"] == "incomplete" and len(recording_transport.calls) == 1


async def test_failed_site_preserves_other_evidence_without_claiming_complete(recording_transport, mist_backend):
    seed_scope(recording_transport)
    recording_transport.reply(403, "GET", "/api/v1/sites/s2/wlans/derived")
    out = await mist_backend.call(TOOL, {"ssid": "Example"})
    assert out["status"] == "incomplete" and len(out["matches"]) == 1
    assert out["failures"][0]["failure"]["status"] == 403


async def test_unmatched_ssid_is_exact_not_a_substring_or_case_fold(recording_transport, mist_backend):
    recording_transport.reply([WLAN], "GET", DERIVED)
    out = await mist_backend.call(TOOL, {"ssid": "example", "site_id": "s1"})
    assert out["status"] == "not_found" and out["matches"] == []


@pytest.mark.parametrize(
    ("changes", "findings"),
    [
        ({"allow_mdns": True}, []),
        ({"limit_bcast": False}, []),
        ({"allow_mdns": None}, []),
        ({"enabled": False, "allow_mdns": True}, ["wlan_disabled"]),
        ({"isolation": True, "allow_mdns": True}, ["client_isolation_enabled"]),
        ({"bonjour": {"services": {"ipp": {"scope": "site"}}}}, ["mdns_filtered_check_bonjour_services"]),
    ],
)
async def test_findings_distinguish_filtering_gateway_and_unknown(changes, findings, recording_transport, mist_backend):
    recording_transport.reply([{**WLAN, **changes}], "GET", DERIVED)
    out = await mist_backend.call(TOOL, {"ssid": "Example", "site_id": "s1"})
    assert out["matches"][0]["findings"] == findings
    assert "end-to-end" in out["unverified"]


async def test_list_wlans_includes_discovery_evidence(recording_transport, mist_backend):
    recording_transport.reply([WLAN], "GET", "/api/v1/sites/s1/wlans")
    out = await mist_backend.call("mist_list_wlans", {"site_id": "s1"})
    (wlan,) = out["wlans"]["items"]
    for key in ("isolation", "l2_isolation", "limit_bcast", "allow_mdns", "allow_ssdp", "bonjour"):
        assert wlan[key] == WLAN[key]


@pytest.mark.parametrize(
    "args",
    [
        {"ssid": " "},
        {"ssid": "Example", "site_id": "s1", "org_id": "o1"},
        {"ssid": "Example", "site_id": "bad/path"},
        {"ssid": "Example", "org_id": "bad/path"},
    ],
)
async def test_invalid_scope_is_refused_before_traffic(args, recording_transport, mist_backend):
    out = await mist_backend.call(TOOL, args)
    assert "error" in out and recording_transport.calls == []


async def test_diagnostic_works_with_writes_pinned_off(recording_transport, mist_client_factory):
    recording_transport.reply([WLAN], "GET", DERIVED)
    client = mist_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    backend = tools.backend(client=lambda: client)
    out = await sdk_compat.call_tool_raw(backend, TOOL, {"ssid": "Example", "site_id": "s1"})
    assert out["status"] == "matched"
    assert [c.method for c in recording_transport.calls] == ["GET"]


@pytest.mark.parametrize(
    "query",
    [
        "why can't my iPhone AirPrint on this SSID",
        "look at mist and ssid Example and see why my iphone cant airprint",
        "Mist Bonjour discovery settings",
        "check mDNS on my WLAN",
        "my printer is not discoverable",
    ],
)
def test_discovery_questions_find_the_compact_read_tool(query):
    (hit,) = find_tool(query, product="mist", top_k=1, include_schema=True)
    assert hit["name"] == TOOL and hit["kind"] == "read"
    assert hit["schema"]["required"] == ["ssid"]
