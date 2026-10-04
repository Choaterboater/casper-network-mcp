"""access_check v2 and the scope gate (Review Focus 3 and 5).

The Mist and ClearPass rules come from Casper's
``docs/patches/hpe-networking-mcp-access-check.patch`` (its tests rewritten for
v2): Mist roles from ``GET /api/v1/self`` privileges, ClearPass from
``/api/oauth/me`` and ``/api/oauth/privileges`` (``#`` = read-only). Central
reports ``unknown`` (Decision 6: no GreenLake in 0.1.0).
"""

from __future__ import annotations

import pytest

SELF = ("GET", "/api/v1/self")
UPDATE = {"site_id": "s2", "wlan_id": "w1", "changes": {"vlan_id": 30}}


def _mist(out):
    return next(p for p in out["products"] if p["product"] == "mist")


def _puts(transport):
    return [c for c in transport.calls if c.method == "PUT"]


async def test_mist_self_privileges_become_scopes(server_with_replies):
    s = server_with_replies(
        {
            SELF: {
                "privileges": [
                    {"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"},
                    {"scope": "org", "org_id": "o1", "name": "Example Org", "role": "read"},
                ]
            }
        }
    )
    out = await s.call("access_check", {})
    assert out["contract"] == "casper/access-check v2"
    mist = _mist(out)
    assert mist["access"] == "read-write"
    assert mist["can_change"] == [{"kind": "site", "id": "s1", "name": "Branch-12"}]
    assert mist["read_only"] == [{"kind": "org", "id": "o1", "name": "Example Org"}]


async def test_write_outside_scope_is_refused_before_sending(server_with_replies, recording_transport):
    s = server_with_replies(
        {
            SELF: {"privileges": [{"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"}]},
            ("GET", "/api/v1/sites/s2"): {"id": "s2", "org_id": "o9"},
        }
    )
    await s.call("access_check", {})
    out = await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert "Branch-12" in out["error"]
    assert _puts(recording_transport) == []


async def test_name_with_odd_characters_drops_the_list(server_with_replies):
    s = server_with_replies(
        {SELF: {"privileges": [{"scope": "site", "site_id": "s1", "name": "Branch<12>", "role": "write"}]}}
    )
    mist = _mist(await s.call("access_check", {}))
    assert "can_change" not in mist
    assert mist["access"] == "read-write"


async def test_missing_login_reported(monkeypatch, server_with_replies):
    for var in ("CLEARPASS_BASE_URL", "CLEARPASS_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    out = await server_with_replies({}).call("access_check", {})
    cp = next(p for p in out["products"] if p["product"] == "clearpass")
    assert cp["access"] == "unknown" and cp["login"] == "missing"


async def test_read_only_pin_still_reports_the_logins_real_reach(server_with_replies):
    s = server_with_replies(
        {SELF: {"privileges": [{"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"}]}},
        read_only=True,
    )
    mist = _mist(await s.call("access_check", {}))
    assert mist["access"] == "read-write"
    assert mist["can_change"] == [{"kind": "site", "id": "s1", "name": "Branch-12"}]
    assert mist["server_gate"] == {"flag": "--read-only", "state": "off"}


async def test_without_the_pin_the_gate_state_is_on(server_with_replies):
    mist = _mist(await server_with_replies({SELF: {"privileges": []}}).call("access_check", {}))
    assert mist["server_gate"] == {"flag": "--read-only", "state": "on"}


async def test_fresh_server_loads_scopes_itself(server_with_replies, recording_transport):
    # no access_check call first (a writes-on restart, or a client other than Casper)
    s = server_with_replies(
        {
            SELF: {
                "privileges": [
                    {"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"},
                    {"scope": "org", "org_id": "o1", "name": "Example Org", "role": "read"},
                ]
            },
            ("GET", "/api/v1/sites/s2"): {"id": "s2", "org_id": "o1", "name": "Branch-40"},
        }
    )
    out = await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert "Branch-12" in out["error"] and "Branch-40" in out["error"]
    assert _puts(recording_transport) == []


async def test_org_write_covers_its_sites(server_with_replies, recording_transport):
    s = server_with_replies(
        {
            SELF: {"privileges": [{"scope": "org", "org_id": "o1", "name": "Example Org", "role": "write"}]},
            ("GET", "/api/v1/sites/s2"): {"id": "s2", "org_id": "o1"},
        }
    )
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert _puts(recording_transport)


async def test_sitegroup_write_covers_member_sites(server_with_replies, recording_transport):
    s = server_with_replies(
        {
            SELF: {
                "privileges": [
                    {"scope": "sitegroup", "sitegroup_id": "g1", "org_id": "o1", "name": "Branches", "role": "write"}
                ]
            },
            ("GET", "/api/v1/sites/s2"): {"id": "s2", "org_id": "o1", "sitegroup_ids": ["g1"]},
        }
    )
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert _puts(recording_transport)


async def test_sitegroup_ids_list_from_the_spec_shape(server_with_replies, recording_transport):
    # The bundled spec's admin_privilege carries sitegroup_ids (a list).
    s = server_with_replies(
        {
            SELF: {
                "privileges": [
                    {"scope": "sitegroup", "sitegroup_ids": ["g1"], "org_id": "o1", "name": "Branches", "role": "admin"}
                ]
            },
            ("GET", "/api/v1/sites/s2"): {"id": "s2", "org_id": "o1", "sitegroup_ids": ["g1"]},
        }
    )
    mist = _mist(await s.call("access_check", {}))
    assert mist["can_change"] == [{"kind": "sitegroup", "id": "g1", "name": "Branches"}]
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert _puts(recording_transport)


async def test_unresolvable_target_goes_to_the_product(server_with_replies, recording_transport):
    s = server_with_replies(
        {
            SELF: {"privileges": [{"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"}]},
            ("GET", "/api/v1/sites/s9"): 404,
        }
    )
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": {**UPDATE, "site_id": "s9"}})
    assert _puts(recording_transport)


async def test_a_failed_scope_fetch_never_refuses(server_with_replies, recording_transport):
    s = server_with_replies({SELF: 500})
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert _puts(recording_transport)


async def test_write_inside_scope_is_sent(server_with_replies, recording_transport):
    s = server_with_replies(
        {SELF: {"privileges": [{"scope": "site", "site_id": "s2", "name": "Branch-40", "role": "write"}]}}
    )
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert [c.url.path for c in _puts(recording_transport)] == ["/api/v1/sites/s2/wlans/w1"]


async def test_scopes_are_loaded_once(server_with_replies, recording_transport):
    s = server_with_replies(
        {SELF: {"privileges": [{"scope": "site", "site_id": "s2", "name": "Branch-40", "role": "write"}]}}
    )
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert [c.url.path for c in recording_transport.calls].count("/api/v1/self") == 1


async def test_org_write_outside_scope_is_refused(server_with_replies, recording_transport):
    s = server_with_replies(
        {SELF: {"privileges": [{"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"}]}}
    )
    out = await s.call(
        "invoke_tool", {"name": "mist_claim_devices", "arguments": {"org_id": "o1", "claim_codes": ["C1"]}}
    )
    assert "Branch-12" in out.get("error", "")
    assert not any(c.method == "POST" for c in recording_transport.calls)


async def test_generated_write_is_scope_checked_too(server_with_replies, recording_transport):
    from casper_network_mcp.router.index import catalog

    s = server_with_replies(
        {
            SELF: {"privileges": [{"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"}]},
            ("GET", "/api/v1/sites/s2"): {"id": "s2", "org_id": "o9", "name": "Branch-40"},
        }
    )
    entry = next(
        e
        for e in catalog().entries.values()
        if e.product == "mist" and e.origin == "generated" and e.method == "PUT" and e.path == "/api/v1/sites/{site_id}"
    )
    out = await s.call("invoke_tool", {"name": entry.name, "arguments": {"site_id": "s2", "body": {"name": "x"}}})
    assert "Branch-12" in str(out)
    assert _puts(recording_transport) == []


async def test_reads_never_trigger_a_scope_fetch(server_with_replies, recording_transport):
    s = server_with_replies({})
    await s.call("invoke_read_tool", {"name": "mist_list_sites", "arguments": {"org_id": "o1"}})
    assert "/api/v1/self" not in [c.url.path for c in recording_transport.calls]


async def test_unknown_role_makes_mist_unknown(server_with_replies):
    s = server_with_replies(
        {SELF: {"privileges": [{"scope": "org", "org_id": "o1", "name": "X", "role": "superuser"}]}}
    )
    mist = _mist(await s.call("access_check", {}))
    assert mist["access"] == "unknown"
    assert "can_change" not in mist and "read_only" not in mist


async def test_read_roles_make_mist_read_only(server_with_replies):
    s = server_with_replies(
        {
            SELF: {
                "email": "ops@example.com",
                "privileges": [
                    {"scope": "org", "org_id": "o1", "name": "Example Org", "role": "read"},
                    {"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "helpdesk"},
                    {"scope": "site", "site_id": "s3", "name": "Branch-13", "role": "installer"},
                ],
            }
        }
    )
    mist = _mist(await s.call("access_check", {}))
    assert mist["access"] == "read-only"
    assert mist["identity"] == "ops@example.com"
    assert mist["role"] == "helpdesk/installer/read"  # "," is not a plain character for Casper
    assert "can_change" not in mist
    assert len(mist["read_only"]) == 3


async def test_more_than_64_scopes_drops_the_list(server_with_replies):
    privileges = [{"scope": "site", "site_id": f"s{i}", "name": f"Branch-{i}", "role": "write"} for i in range(65)]
    mist = _mist(await server_with_replies({SELF: {"privileges": privileges}}).call("access_check", {}))
    assert mist["access"] == "read-write" and "can_change" not in mist


async def test_msp_privilege_turns_off_scope_refusals(server_with_replies, recording_transport):
    s = server_with_replies(
        {
            SELF: {
                "privileges": [
                    {"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"},
                    {"scope": "msp", "msp_id": "m1", "name": "Example MSP", "role": "admin"},
                ]
            },
            ("GET", "/api/v1/sites/s2"): {"id": "s2", "org_id": "o9"},
        }
    )
    mist = _mist(await s.call("access_check", {}))
    assert mist["access"] == "read-write" and "can_change" not in mist
    await s.call("invoke_tool", {"name": "mist_update_wlan", "arguments": UPDATE})
    assert _puts(recording_transport)


@pytest.mark.parametrize(
    ("privileges", "access"),
    [(["#cppm_config", "#guest_users"], "read-only"), (["#cppm_config", "guest_users"], "read-write"), ([], "unknown")],
)
async def test_clearpass_privileges(server_with_replies, privileges, access):
    s = server_with_replies(
        {
            ("GET", "/api/oauth/me"): {"info": "API operator", "name": "api-client"},
            ("GET", "/api/oauth/privileges"): {"privileges": privileges},
        },
        environ={"CLEARPASS_BASE_URL": "https://198.51.100.20", "CLEARPASS_API_TOKEN": "t"},
    )
    cp = next(p for p in (await s.call("access_check", {}))["products"] if p["product"] == "clearpass")
    assert cp["access"] == access
    assert "login" not in cp
    if access != "unknown":
        assert cp["identity"] == "API operator"


async def test_central_is_unknown_and_makes_no_call(server_with_replies, recording_transport):
    s = server_with_replies(
        {},
        environ={
            "CENTRAL_BASE_URL": "https://de1.api.central.arubanetworks.com",
            "CENTRAL_CLIENT_ID": "i",
            "CENTRAL_CLIENT_SECRET": "x",
        },
    )
    central = next(p for p in (await s.call("access_check", {}))["products"] if p["product"] == "central")
    assert central["access"] == "unknown" and "login" not in central
    assert recording_transport.calls == [] and recording_transport.token_calls == []


async def test_access_check_never_returns_a_token(server_with_replies):
    s = server_with_replies(
        {SELF: {"privileges": [{"scope": "site", "site_id": "s1", "name": "Branch-12", "role": "write"}]}}
    )
    assert "test-token" not in str(await s.call("access_check", {}))


async def test_mist_login_missing_after_all(server_with_replies):
    out = await server_with_replies({}, environ={}).call("access_check", {})
    assert {p["product"]: p.get("login") for p in out["products"]} == {
        "central": "missing",
        "mist": "missing",
        "clearpass": "missing",
    }


async def test_mist_401_reports_login_expired(server_with_replies):
    out = await server_with_replies({SELF: 401}).call("access_check", {})
    mist = _mist(out)
    assert mist["access"] == "unknown" and mist["login"] == "expired"


async def test_clearpass_401_reports_login_expired(server_with_replies):
    s = server_with_replies(
        {("GET", "/api/oauth/me"): 401, ("GET", "/api/oauth/privileges"): 401},
        environ={"CLEARPASS_BASE_URL": "https://198.51.100.20", "CLEARPASS_API_TOKEN": "t"},
    )
    cp = next(p for p in (await s.call("access_check", {}))["products"] if p["product"] == "clearpass")
    assert cp["access"] == "unknown" and cp["login"] == "expired"


async def test_other_failures_do_not_say_expired(server_with_replies):
    mist = _mist(await server_with_replies({SELF: 403}).call("access_check", {}))
    assert mist["access"] == "unknown" and "login" not in mist


async def test_tool_call_on_401_says_the_login_expired(server_with_replies):
    s = server_with_replies({("GET", "/api/v1/orgs/o1/sites"): 401})
    out = await s.call("invoke_read_tool", {"name": "mist_list_sites", "arguments": {"org_id": "o1"}})
    assert out == {"error": "login_expired", "product": "mist"}


async def test_clearpass_tool_call_on_401_says_the_login_expired(server_with_replies):
    s = server_with_replies(
        {("GET", "/api/oauth/me"): 401},
        environ={"CLEARPASS_BASE_URL": "https://198.51.100.20", "CLEARPASS_API_TOKEN": "t"},
    )
    out = await s.call("invoke_read_tool", {"name": "clearpass_get", "arguments": {"path": "/api/oauth/me"}})
    assert out == {"error": "login_expired", "product": "clearpass"}
