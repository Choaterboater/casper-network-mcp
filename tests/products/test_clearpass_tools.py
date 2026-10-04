"""ClearPass hand-written tools: no confirm step, opt-in previews, spec-correct paths.

Behaviour ported from hpe-networking-mcp ``tests/unit/test_clearpass_backend.py``
(MIT), rewritten for the gated client and the recording fake.
"""

from __future__ import annotations

import inspect
import json

import pytest
from conftest import Backend, body_of

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.products.clearpass import tools as clearpass


@pytest.fixture
def clearpass_backend(recording_transport, clearpass_client_factory) -> Backend:
    client = clearpass_client_factory(gate=Gate(read_only=False), transport=recording_transport)
    return Backend(clearpass.backend(client=lambda: client))


def _params(request) -> dict[str, str]:
    return dict(request.url.params)


def test_no_confirm_argument_anywhere():
    for t in clearpass.backend_tools():
        assert "confirm" not in inspect.signature(t.fn).parameters, t.name


def test_every_change_has_an_opt_in_preview():
    from casper_network_mcp.core.kinds import labels

    rows = labels("clearpass")
    for t in clearpass.backend_tools():
        params = inspect.signature(t.fn).parameters
        if rows[t.name]["kind"] == "read":
            assert "dry_run" not in params, t.name
        else:
            assert params["dry_run"].default is False, t.name


def test_no_generic_write_passthrough_or_status_tool():
    names = {t.name for t in clearpass.backend_tools()}
    assert "clearpass_write" not in names and "clearpass_status" not in names
    assert {"clearpass_get", "clearpass_list_access_tracker_sessions", "clearpass_who_am_i"} <= names


async def test_oauth_me_path_builds_correctly(recording_transport, clearpass_backend):
    recording_transport.reply({"info": "Example API client"}, "GET", "/api/oauth/me")
    out = await clearpass_backend.call("clearpass_who_am_i", {})
    assert recording_transport.calls[0].url.path == "/api/oauth/me"
    assert out["me"] == {"info": "Example API client"}


async def test_a_write_with_dry_run_sends_nothing(recording_transport, clearpass_backend):
    out = await clearpass_backend.call("clearpass_delete_guest", {"username": "visitor1", "dry_run": True})
    assert out["would_send"] == {"method": "DELETE", "path": "/api/guest/username/visitor1", "body": None}
    assert recording_transport.calls == []


async def test_read_only_pin_refuses_a_change(recording_transport, clearpass_client_factory):
    client = clearpass_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    backend = clearpass.backend(client=lambda: client)
    out = await sdk_compat.call_tool_raw(backend, "clearpass_delete_endpoint", {"mac_address": "00:00:5e:00:53:01"})
    assert "read-only" in out["error"] and recording_transport.calls == []


async def test_clearpass_get_stays_under_api_and_bounds_lists(recording_transport, clearpass_backend):
    for bad in ("/admin", "/api/%2e%2e/admin", "/api/%252e%252e/admin"):
        assert "error" in await clearpass_backend.call("clearpass_get", {"path": bad})
    assert recording_transport.calls == []
    recording_transport.reply({"_embedded": {"items": [{"id": i} for i in range(9)]}}, "GET", "/api/endpoint")
    out = await clearpass_backend.call("clearpass_get", {"path": "/api/endpoint", "limit": 2})
    assert out["_pagination"]["truncated"] is True


async def test_endpoint_by_mac_normalises_and_compacts(recording_transport, clearpass_backend):
    recording_transport.reply(
        {"id": 7, "mac_address": "00005e005301", "status": "Known", "noise": 1},
        "GET",
        "/api/endpoint/mac-address/00005e005301",
    )
    out = await clearpass_backend.call("clearpass_get_endpoint_by_mac", {"mac_address": "00-00-5E-00-53-01"})
    assert out["normalized_mac"] == "00005e005301"
    assert out["endpoint"] == {"id": 7, "mac_address": "00005e005301", "status": "Known"}


async def test_auth_failures_filter_and_compact(recording_transport, clearpass_backend):
    recording_transport.reply(
        {"_embedded": {"items": [{"id": "s1", "username": "u1", "auth_error": "bad cert"}]}}, "GET", "/api/session"
    )
    out = await clearpass_backend.call("clearpass_list_auth_failures", {"limit": 5})
    params = _params(recording_transport.calls[0])
    assert json.loads(params["filter"]) == {"auth_status": "FAILED"}
    assert params["limit"] == "5" and params["sort"] == "-acctstarttime"
    assert out["sessions"]["items"] == [{"id": "s1", "username": "u1", "reason": "bad cert"}]


async def test_network_device_by_name(recording_transport, clearpass_backend):
    await clearpass_backend.call("clearpass_get_network_device", {"name": "Branch-12 switch"})
    assert recording_transport.calls[0].url.raw_path == b"/api/network-device/name/Branch-12%20switch"
    out = await clearpass_backend.call("clearpass_get_network_device", {})
    assert "exactly one" in out["error"]


async def test_find_guest_by_email_uses_a_filter(recording_transport, clearpass_backend):
    await clearpass_backend.call("clearpass_find_guest", {"query": "visitor@example.com", "field": "email"})
    call = recording_transport.calls[0]
    assert call.url.path == "/api/guest"
    assert json.loads(_params(call)["filter"]) == {"email": "visitor@example.com"}


async def test_endpoint_attribute_patch_matches_the_spec(recording_transport, clearpass_backend):
    out = await clearpass_backend.call(
        "clearpass_update_endpoint_attributes",
        {"mac_address": "00:00:5e:00:53:01", "attributes": {"Owner": "Example"}, "dry_run": True},
    )
    assert out["would_send"]["method"] == "PATCH"
    assert out["would_send"]["path"] == "/api/endpoint/mac-address/00005e005301"
    assert "params" not in out["would_send"]  # the spec's PATCH has no change_of_authorization query


async def test_guest_enable_needs_one_identifier(recording_transport, clearpass_backend):
    out = await clearpass_backend.call("clearpass_set_guest_enabled", {"enabled": False})
    assert "exactly one" in out["error"] and recording_transport.calls == []
    await clearpass_backend.call("clearpass_set_guest_enabled", {"enabled": False, "guest_id": "42"})
    call = recording_transport.calls[0]
    assert (call.method, call.url.path) == ("PATCH", "/api/guest/42")
    assert body_of(call) == {"enabled": False} and _params(call) == {"change_of_authorization": "false"}


async def test_create_guest_hides_the_password_in_the_preview(recording_transport, clearpass_backend):
    out = await clearpass_backend.call(
        "clearpass_create_guest", {"username": "visitor1", "password": "s3cret", "dry_run": True}
    )
    assert out["would_send"]["body"] == {"username": "visitor1", "password": "[hidden]"}


async def test_disconnect_and_service_toggle(recording_transport, clearpass_backend):
    await clearpass_backend.call("clearpass_disconnect_session", {"session_id": "abc"})
    await clearpass_backend.call("clearpass_set_service_enabled", {"name": "Guest Access", "enabled": False})
    first, second = recording_transport.calls
    assert (first.method, first.url.path) == ("POST", "/api/session/abc/disconnect")
    assert (second.method, second.url.raw_path) == ("PATCH", b"/api/config/service/name/Guest%20Access/disable")


async def test_insight_and_onguard_paths(recording_transport, clearpass_backend):
    await clearpass_backend.call("clearpass_get_insight_endpoint", {"mac_address": "00:00:5e:00:53:01"})
    await clearpass_backend.call("clearpass_get_onguard_activity_by_mac", {"mac_address": "00:00:5e:00:53:01"})
    assert [c.url.path for c in recording_transport.calls] == [
        "/api/insight/endpoint/mac/00005e005301",
        "/api/onguard-activity/host_mac/00005e005301",
    ]


async def test_cluster_servers_sends_no_paging_the_spec_lacks(recording_transport, clearpass_backend):
    recording_transport.reply(
        {"_embedded": {"items": [{"name": "cp1"}, {"name": "cp2"}]}}, "GET", "/api/cluster/server"
    )
    out = await clearpass_backend.call("clearpass_list_cluster_servers", {"limit": 1})
    assert _params(recording_transport.calls[0]) == {}
    assert out["cluster_servers"]["items"] == [{"name": "cp1"}]


async def test_an_id_with_a_slash_is_refused(recording_transport, clearpass_backend):
    out = await clearpass_backend.call("clearpass_get_access_tracker_session", {"session_id": "a/../b"})
    assert "error" in out and recording_transport.calls == []


def test_kinds_are_honest():
    from casper_network_mcp.core.kinds import labels

    rows = labels("clearpass")
    assert rows["clearpass_disconnect_session"]["kind"] == "disruptive"
    assert rows["clearpass_delete_guest"]["kind"] == "delete"
    assert rows["clearpass_delete_endpoint"]["kind"] == "delete"
    assert rows["clearpass_who_am_i"]["kind"] == "read"
