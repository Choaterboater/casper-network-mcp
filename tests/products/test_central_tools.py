"""Central hand-written tools: one gated road to Central, no confirm step, no lab names.

The plan's Task 8 tests, plus checks over all ~250 copied tools: every request
passes the gate, a preview (``dry_run=True``) never sends a change, no tool
asks the person anything or takes a ``confirm`` argument, and the org name
and the MAC Address Store are never constants.
"""

from __future__ import annotations

import inspect

import pytest
from conftest import Backend, body_of, placeholder_args

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products.central import compat
from casper_network_mcp.products.central import tools as central

STORE = "/network-config/v1alpha1/identity-stores"


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(compat, "POLL_INTERVAL", 0)
    monkeypatch.setattr(compat, "POLL_MAX", 2)


class CentralBackends:
    def __init__(self, servers):
        self.servers = dict(servers)

    def all_tools(self):
        for server in self.servers.values():
            yield from sdk_compat.tool_registry(server).values()

    def server_of(self, name):
        return next(s for s in self.servers.values() if sdk_compat.get_tool(s, name) is not None)

    async def call(self, name, args):
        return await sdk_compat.call_tool_raw(self.server_of(name), name, args)


@pytest.fixture
def central_backends(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=False), transport=recording_transport)
    return CentralBackends(central.backends(client=lambda: client))


@pytest.fixture
def central_backend(central_backends):
    return Backend(central_backends.server_of("create_mac_auth_profile"))


def test_four_backends_hold_every_labelled_tool(central_backends):
    from casper_network_mcp.core.kinds import labels

    assert set(central_backends.servers) == {"central-monitoring", "central-config", "central-ops", "central-nac"}
    names = [t.name for t in central_backends.all_tools()]
    assert len(names) == len(set(names))
    assert set(names) == set(labels("central"))


def test_no_confirm_and_no_question_to_the_person(central_backends):
    for tool in central_backends.all_tools():
        params = inspect.signature(tool.fn).parameters
        assert "confirm" not in params, tool.name
        assert "ctx" not in params, tool.name
        if "dry_run" in params:
            assert params["dry_run"].default is False, tool.name
        assert tool.context_kwarg is None, tool.name


async def test_auth_profile_uses_the_org_name_it_was_given(central_backend, recording_transport):
    out = await central_backend.call(
        "create_mac_auth_profile", {"name": "p1", "organization_name": "Example Org", "dry_run": True}
    )
    assert out["would_send"]["body"]["organization-name"] == "Example Org"
    assert recording_transport.calls == []


def test_org_name_is_a_required_argument(central_backends):
    for name in ("create_mac_auth_profile", "create_dot1x_auth_profile"):
        schema = central_backends.server_of(name)
        assert "organization_name" in sdk_compat.get_tool(schema, name).parameters["required"], name


async def test_get_site_says_when_no_site_matched(central_backends, monkeypatch):
    from casper_network_mcp.products.central import monitoring

    class _NoSites:
        def get_sites(self, limit=100, offset=0):
            return []

    monkeypatch.setattr(monitoring, "get_mcp_client", lambda: _NoSites())
    out = await central_backends.call("get_site", {"name": "No Such"})
    assert out["found"] is False and out["name"] == "No Such"


async def test_mac_store_is_looked_up_by_name(central_backend, recording_transport):
    store_id = "00000000-0000-0000-0000-000000000005"
    recording_transport.reply(
        {
            "store": [
                {"id": "00000000-0000-0000-0000-000000000004", "name": "Example LDAP"},
                {"id": store_id, "name": "MAC Address Store"},
            ]
        },
        "GET",
        STORE,
    )
    await central_backend.call(
        "create_mac_auth_profile", {"name": "p1", "organization_name": "Example Org", "networks": ["Guest"]}
    )
    post = next(c for c in recording_transport.calls if c.method == "POST")
    assert body_of(post)["identity-stores"] == [store_id]
    assert body_of(post)["networks"] == ["Guest"]


async def test_missing_mac_store_is_a_plain_error(central_backend, recording_transport):
    recording_transport.reply({"store": []}, "GET", STORE)
    out = await central_backend.call("create_mac_auth_profile", {"name": "p1", "organization_name": "Example Org"})
    assert "MAC Address Store" in out["error"]
    assert all(c.method == "GET" for c in recording_transport.calls)


async def test_every_central_tool_reaches_the_network_only_through_the_gate(
    recording_transport, central_backends, monkeypatch
):
    checked = []
    real = Gate.check

    def spy(self, *a, **k):
        checked.append((a[1], a[2]))
        return real(self, *a, **k)

    monkeypatch.setattr(Gate, "check", spy)
    for tool in central_backends.all_tools():  # sync and async tools alike
        await central_backends.call(tool.name, placeholder_args(tool))
    sent = {(c.method, c.url.raw_path.decode().split("?")[0]) for c in recording_transport.calls}
    assert sent <= set(checked)
    assert recording_transport.calls  # the walk really reached the fake


async def test_a_preview_never_sends_a_change(recording_transport, central_backends):
    previewed = []
    for tool in central_backends.all_tools():
        if "dry_run" not in inspect.signature(tool.fn).parameters:
            continue
        before = len(recording_transport.calls)
        await central_backends.call(tool.name, {**placeholder_args(tool), "dry_run": True})
        changes = [c for c in recording_transport.calls[before:] if c.method != "GET"]
        assert not changes, (tool.name, [(c.method, c.url.path) for c in changes])
        previewed.append(tool.name)
    assert len(previewed) > 40


async def test_read_only_pin_refuses_a_bounce_but_runs_a_ping(recording_transport, central_client_factory):
    client = central_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    servers = CentralBackends(central.backends(client=lambda: client))
    out = await servers.call("poe_bounce", {"serial_number": "SN1", "ports": ["1/1/1"], "device_type": "CX"})
    assert "read-only" in str(out) and recording_transport.calls == []
    await servers.call("cx_ping", {"serial_number": "SN1", "destination": "192.0.2.1"})
    assert recording_transport.calls[0].url.path == "/network-troubleshooting/v1/cx/SN1/ping"


async def test_alert_actions_send_without_asking(recording_transport, central_backends):
    out = await central_backends.call(
        "clear_alerts", {"keys": ["k1"], "reason": "Problem was resolved", "dry_run": True}
    )
    assert out["would_send"]["path"] == "/network-notifications/v1/alerts/clear"
    assert recording_transport.calls == []
    await central_backends.call("clear_alerts", {"keys": ["k1"], "reason": "Problem was resolved"})
    assert recording_transport.calls[0].method == "POST"


async def test_switch_group_is_an_argument(recording_transport, central_backends):
    tool = sdk_compat.get_tool(central_backends.server_of("push_aruba_device_profiles"), "push_aruba_device_profiles")
    assert tool.parameters["properties"]["switch_group_name"]["default"] == "Switches"


def test_no_lab_constants_in_central_code():
    import pathlib

    import casper_network_mcp.products.central as pkg

    for p in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "_CENTRAL_ORG_NAME" not in text, p.name
        assert "_MAC_ADDRESS_STORE_ID" not in text, p.name
        assert "credentials.yaml" not in text, p.name


# ── behaviour ported from hpe-networking-mcp's Central tests (recording fake, no monkeypatched clients) ──

TS = "/network-troubleshooting/v1"


async def test_cx_ping_starts_and_polls_the_task(recording_transport, central_backends):
    import httpx

    recording_transport.reply(
        httpx.Response(202, headers={"Location": "/x/async-operations/t1"}), "POST", f"{TS}/cx/SN1/ping"
    )
    recording_transport.reply({"status": "COMPLETED", "output": "ok"}, "GET", f"{TS}/cx/SN1/ping/async-operations/t1")
    out = await central_backends.call("cx_ping", {"serial_number": "SN1", "destination": "192.0.2.1", "count": 3})
    assert out["status"] == "COMPLETED"
    assert body_of(recording_transport.calls[0]) == {"destination": "192.0.2.1", "count": 3}


async def test_cx_show_checks_commands_before_sending(recording_transport, central_backends):
    out = await central_backends.call("cx_show", {"serial_number": "SN1", "commands": ["configure terminal"]})
    assert recording_transport.calls == [] and "error" in str(out).lower()


async def test_bounces_refuse_access_points_before_sending(recording_transport, central_backends):
    for name in ("poe_bounce", "port_bounce"):
        out = await central_backends.call(name, {"serial_number": "SN1", "ports": ["1"], "device_type": "AP"})
        assert "not supported on Access Points" in out["errors"][0]
    assert recording_transport.calls == []


async def test_port_bounce_preview(recording_transport, central_backends):
    out = await central_backends.call(
        "port_bounce", {"serial_number": "SN1", "ports": ["1/1/7"], "device_type": "CX", "dry_run": True}
    )
    assert out["would_send"] == {"method": "POST", "path": f"{TS}/cx/SN1/portBounce", "body": {"ports": ["1/1/7"]}}
    assert recording_transport.calls == []


async def test_central_get_stays_on_reviewed_reads(recording_transport, central_backends):
    for bad in (
        "/network-config/v1/global",
        "/network-monitoring/v1/../x",
        "https://example.org/network-monitoring/v1/x",
    ):
        out = await central_backends.call("central_get", {"path": bad})
        assert "error" in out, bad
    assert recording_transport.calls == []
    recording_transport.reply({"items": [{"id": i} for i in range(9)]}, "GET", "/network-monitoring/v1/aps")
    out = await central_backends.call("central_get", {"path": "/network-monitoring/v1/aps", "limit": 3})
    assert len(out["data"]["items"]) == 3


async def test_report_create_preview_then_send(recording_transport, central_backends):
    body = {"name": "Weekly", "type": "x"}
    out = await central_backends.call("create_report", {"body": body, "dry_run": True})
    assert out["dry_run"] is True and recording_transport.calls == []
    recording_transport.reply((400, {"errors": ["bad type"]}), "POST", "/network-reporting/v1/reports")
    out = await central_backends.call("create_report", {"body": body})
    assert "error" in out and recording_transport.calls[0].method == "POST"


async def test_firmware_compliance_updates_when_it_exists(recording_transport, central_backends):
    path = "/network-config/v1alpha1/firmware-compliance"
    recording_transport.reply((412, {"detail": "exists"}), "POST", path)
    out = await central_backends.call(
        "set_firmware_compliance",
        {"scope_id": "42", "device_function": "ACCESS_SWITCH", "firmware_version": "10.16.1030"},
    )
    assert [c.method for c in recording_transport.calls] == ["POST", "PATCH"]
    assert out["action"] == "updated"


async def test_bulk_site_delete_preview_and_limits(recording_transport, central_backends):
    out = await central_backends.call("delete_sites_bulk", {"site_ids": ["1", "2"], "dry_run": True})
    assert out["payload"] == {"items": [{"id": "1"}, {"id": "2"}]} and recording_transport.calls == []
    out = await central_backends.call("delete_sites_bulk", {"site_ids": [str(i) for i in range(101)]})
    assert "100" in out["error"] and recording_transport.calls == []


async def test_vsf_template_checks_members(recording_transport, central_backends):
    out = await central_backends.call("build_vsf_template", {"name": "t", "number_of_members": 11, "scope_id": "42"})
    assert "between 1 and 10" in out["error"] and recording_transport.calls == []


async def test_mpsk_and_visitor_lists_are_bounded(recording_transport, central_backends):
    recording_transport.reply(
        {"items": [{"id": i} for i in range(80)]}, "GET", "/network-config/v1alpha1/cnac-named-mpsk-reg"
    )
    out = await central_backends.call("list_mpsk_registrations", {"limit": 5})
    assert len(out["items"]) == 5


async def test_role_write_fails_closed_on_an_error_reply(recording_transport, central_backends):
    recording_transport.reply((500, {"detail": "boom"}), "DELETE", "/network-config/v1alpha1/roles/Guest")
    out = await central_backends.call("delete_role", {"name": "Guest"})
    assert "error" in out or out.get("errors")


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("delete_role", {"name": "Guest/../../x"}),
        ("delete_role", {"name": "a/b"}),
        ("get_auth_server", {"name": "x?y=1"}),
        ("delete_static_tag", {"tag_id": "t#1"}),
        ("get_device_health", {"serial_number": "SN1/../../x"}),
        ("get_audit_log", {"audit_id": "a/b"}),
    ],
)
async def test_an_id_with_a_slash_or_query_is_refused_before_sending(name, args, recording_transport, central_backends):
    out = await central_backends.call(name, args)
    assert "error" in str(out).lower()
    bad = next(iter(args.values()))
    sent = [c.url.raw_path.decode() for c in recording_transport.calls]
    assert not any(bad.split("/")[0] + "/" in p or "?y=" in p or "#" in p for p in sent), sent
    assert all(c.method == "GET" for c in recording_transport.calls)


# ── review fixes: reboot and disconnect, AOS-S reboot, ids with a slash, sync lookups ──

INVENTORY = "/network-monitoring/v1/device-inventory"


@pytest.mark.parametrize("headers", [{}, {"Location": "/x/async-operations/task-1"}])
async def test_reboot_is_one_post_and_a_2xx_is_success(headers, recording_transport, central_backends):
    import httpx

    recording_transport.reply(httpx.Response(202, headers=headers, json={}), "POST", f"{TS}/aps/SN1/reboot")
    out = await central_backends.call("reboot_device", {"serial_number": "SN1", "device_type": "AP"})
    assert [(c.method, c.url.path) for c in recording_transport.calls] == [("POST", f"{TS}/aps/SN1/reboot")]
    assert out["errors"] == [] and out["status_code"] == 202


@pytest.mark.parametrize("headers", [{}, {"Location": "/x/async-operations/task-1"}])
async def test_disconnect_is_one_post_and_a_2xx_is_success(headers, recording_transport, central_backends):
    import httpx

    path = f"{TS}/aps/SN1/disconnectUserByMacAddress"
    recording_transport.reply(httpx.Response(202, headers=headers, json={}), "POST", path)
    out = await central_backends.call("disconnect_client", {"mac_address": "00:00:5e:00:53:01", "ap_serial": "SN1"})
    assert [(c.method, c.url.path) for c in recording_transport.calls] == [("POST", path)]
    assert out["errors"] == [] and out["status_code"] == 202


async def test_reboot_reports_an_error_reply(recording_transport, central_backends):
    recording_transport.reply((403, {"detail": "no"}), "POST", f"{TS}/aps/SN1/reboot")
    out = await central_backends.call("reboot_device", {"serial_number": "SN1", "device_type": "AP"})
    assert out["errors"] and "403" in out["errors"][0]


@pytest.mark.parametrize("device_type", [None, "SWITCH"])
async def test_reboot_sends_an_aos_s_switch_to_aos_s(device_type, recording_transport, central_backends):
    recording_transport.reply(
        {"items": [{"serialNumber": "SN9", "deviceType": "SWITCH", "firmwareVersion": "16.11.0012", "model": "2930F"}]},
        "GET",
        INVENTORY,
    )
    args = {"serial_number": "SN9"} | ({"device_type": device_type} if device_type else {})
    await central_backends.call("reboot_device", args)
    posts = [c.url.path for c in recording_transport.calls if c.method == "POST"]
    assert posts == [f"{TS}/aos-s/SN9/reboot"]


async def test_delete_config_assignment_refuses_a_slash_in_any_piece(recording_transport, central_backends):
    base = {"scope_id": "1001", "device_function": "x1", "profile_type": "x1", "profile_instance": "x1"}
    for name in base:
        out = await central_backends.call("delete_config_assignment", base | {name: "zz9/qq8"})
        assert "error" in str(out).lower(), name
    assert recording_transport.calls == []


# ── minor review fixes ──


def test_no_tool_text_cites_the_tech_docs_or_skips_the_approval_box(central_backends):
    from casper_network_mcp.products.central import config as central_config

    out = central_config.get_config_rollback_status()
    assert not any("tech-docs" in c for c in out["citation"])
    for tool in central_backends.all_tools():
        doc = inspect.getdoc(tool.fn) or ""
        assert "tech-docs" not in doc, tool.name
        assert "no confirmation required" not in doc.lower(), tool.name
        assert "requires_confirmation" not in doc, tool.name


def test_troubleshooting_plan_files_cable_test_as_a_check_and_has_no_confirm_step(monkeypatch):
    from casper_network_mcp.products.central import monitoring

    monkeypatch.setattr(
        monitoring, "find_device", lambda serial: {"serial": serial, "deviceType": "SWITCH", "status": "DOWN"}
    )
    monkeypatch.setattr(monitoring, "get_device_health", lambda serial: {"health": [{"configStatus": "NOT_SYNCED"}]})
    monkeypatch.setattr(monitoring, "get_device_config_issues", lambda serial: {"items": [{"id": "i1"}]})
    monkeypatch.setattr(
        monitoring,
        "list_events",
        lambda serial, hours, limit: {"items": [{"eventName": "link down", "description": "cable poe interface"}]},
    )
    monkeypatch.setattr(monitoring, "list_active_alerts", lambda site_id, limit: {"items": []})

    plan = monitoring.plan_device_troubleshooting("SN1")
    names = lambda bucket: [a["name"] for a in plan[bucket]]
    assert "cable_test" in names("recommended_diagnostics")
    assert "cable_test" not in names("recommended_destructive")
    cable = next(a for a in plan["recommended_diagnostics"] if a["name"] == "cable_test")
    assert "briefly takes the tested port's link down" in str(cable)
    assert "execute_config_health_remediation" in names("recommended_writes")
    text = str(plan)
    assert "requires_confirmation" not in text
    assert "dry_run" not in text
