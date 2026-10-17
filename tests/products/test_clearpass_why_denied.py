"""Synthetic data only; no ClearPass login or network traffic."""

from __future__ import annotations

import json

import pytest
from conftest import Backend

from casper_network_mcp.core.budget import RESPONSE_BUDGET_BYTES
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.kinds import labels
from casper_network_mcp.products.clearpass import tools
from casper_network_mcp.router import dispatch, find

MAC = "02:00:00:00:00:01"
NORMALIZED = "020000000001"
ENDPOINT = f"/api/endpoint/mac-address/{NORMALIZED}"


@pytest.fixture
def backend(recording_transport, clearpass_client_factory):
    client = clearpass_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    return Backend(tools.backend(client=lambda: client))


def session(**extra):
    return {
        "id": "session-1",
        "username": "user1",
        "mac_address": NORMALIZED,
        "acctstarttime": "2025-01-02T12:00:00Z",
        "servicetype": "Login-User",
        "nasipaddress": "192.0.2.1",
        "nas_name": "nad-1",
        "state": "closed",
        "acctterminatecause": "User-Request",
        "tipsrole": "role-1",
        **extra,
    }


def reply_sessions(transport, rows):
    transport.reply({"_embedded": {"items": rows}}, "GET", "/api/session")


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"mac": MAC, "username": "user1"},
        {"username": ""},
        {"username": "  "},
        {"mac": "02:00:00:00:00"},
        {"mac": "02:00:00:00:00:0g"},
        {"mac": "garbage020000000001"},
        {"mac": "02:00-00:00:00:01"},
        {"mac": MAC, "start": "yesterday"},
        {"mac": MAC, "start": "2025-01-01"},
        {"mac": MAC, "end": "2025-01-01T00:00:00"},
        {"mac": MAC, "start": "2025-02-30T00:00:00Z"},
        {"mac": MAC, "start": "2025-01-03T00:00:00Z", "end": "2025-01-01T00:00:00Z"},
    ],
)
async def test_invalid_summary_arguments_send_nothing(backend, recording_transport, args):
    out = await backend.call("clearpass_why_denied", args)
    assert "error" in out
    assert any(word in out["error"] for word in ("exactly one", "blank", "mac", "ISO 8601", "start"))
    assert recording_transport.calls == []


@pytest.mark.parametrize("name", ["clearpass_list_access_tracker_sessions", "clearpass_list_auth_failures"])
@pytest.mark.parametrize("args", [{"mac": "not-a-mac"}, {"start": "2025-01-01"}, {"nad": " "}])
async def test_list_validation(backend, recording_transport, name, args):
    assert "error" in await backend.call(name, args)
    assert recording_transport.calls == []


@pytest.mark.parametrize("mac", [MAC, "02-00-00-00-00-01", "0200.0000.0001", NORMALIZED])
async def test_summary_shape_uses_spec_accounting_fields(backend, recording_transport, mac):
    reply_sessions(recording_transport, [session()])
    recording_transport.reply({"status": "Known", "profile": {"device_category": "Computer"}}, "GET", ENDPOINT)
    out = await backend.call("clearpass_why_denied", {"mac": mac})
    assert out["endpoint"] == {"status": "Known", "profile": {"device_category": "Computer"}}
    assert out["endpoint_mac"] == NORMALIZED
    assert out["sessions"] == [
        {
            "id": "session-1",
            "username": "user1",
            "mac": NORMALIZED,
            "time": "2025-01-02T12:00:00Z",
            "service": "Login-User",
            "nad": {"ip": "192.0.2.1", "name": "nad-1"},
            "accounting_state": "closed",
            "termination_cause": "User-Request",
            "roles": {"tipsrole": "role-1"},
            "auth_result": None,
            "reason": None,
            "enforcement_profile": None,
        }
    ]
    assert "not full Access Tracker rejects" in out["limitation"]
    assert out["degraded"] is False
    first, second = recording_transport.calls
    assert json.loads(first.url.params["filter"]) == {"mac_address": NORMALIZED}
    assert first.url.params["sort"] == "-acctstarttime"
    assert first.url.params["limit"] == "5"
    assert second.url.params["profile_details"] == "true"
    assert all(call.method == "GET" for call in recording_transport.calls)


async def test_returned_reason_result_and_enforcement_not_inferred(backend, recording_transport):
    reply_sessions(
        recording_transport,
        [
            session(
                auth_status="REJECT",
                auth_error="Certificate expired",
                enforcement_profile="deny-profile",
                arubauserrole="deny-role",
                noise="discard",
            )
        ],
    )
    out = await backend.call("clearpass_why_denied", {"username": "user1"})
    row = out["sessions"][0]
    assert (row["auth_result"], row["reason"], row["enforcement_profile"]) == (
        "REJECT",
        "Certificate expired",
        "deny-profile",
    )
    assert row["roles"]["arubauserrole"] == "deny-role"
    assert "noise" not in row
    assert recording_transport.calls[1].url.path == ENDPOINT
    assert json.loads(recording_transport.calls[0].url.params["filter"]) == {"username": "user1"}


async def test_username_without_mac_does_not_guess_endpoint(backend, recording_transport):
    reply_sessions(recording_transport, [session(mac_address=None, callingstationid="not-a-mac")])
    out = await backend.call("clearpass_why_denied", {"username": "user1"})
    assert out["endpoint"] is None and out["endpoint_mac"] is None
    assert len(recording_transport.calls) == 1


@pytest.mark.parametrize("args", [{"mac": MAC}, {"username": "user1"}])
async def test_empty_sessions_are_explicit(backend, recording_transport, args):
    reply_sessions(recording_transport, [])
    recording_transport.reply((404, {"detail": "Not found"}), "GET", ENDPOINT)
    out = await backend.call("clearpass_why_denied", args)
    assert out["sessions"] == []
    assert "No matching sessions" in out["message"]
    if "mac" in args:
        assert out["degraded"] and "error" in out["endpoint"]
    else:
        assert out["endpoint"] is None


async def test_redaction_applies_to_endpoint_and_summary(backend, recording_transport):
    reply_sessions(
        recording_transport,
        [
            session(
                enforcement_profile={"name": "profile-1", "shared_secret": "synthetic-secret"},
                auth_error=json.dumps({"password": "synthetic-password", "message": "Rejected"}),
            )
        ],
    )
    recording_transport.reply({"profile": {"name": "profile-1", "api_token": "synthetic-token"}}, "GET", ENDPOINT)
    out = await backend.call("clearpass_why_denied", {"mac": MAC})
    text = json.dumps(out)
    assert "synthetic-secret" not in text and "synthetic-password" not in text and "synthetic-token" not in text
    assert "[hidden]" in text and "Rejected" in text


@pytest.mark.parametrize("name", ["clearpass_list_access_tracker_sessions", "clearpass_list_auth_failures"])
async def test_filters_and_inclusive_timezone_window(backend, recording_transport, name):
    reply_sessions(recording_transport, [session()])
    args = {
        "mac": MAC,
        "username": "user1",
        "nad": "192.0.2.1",
        "service": "Login-User",
        "start": "2025-01-02T13:00:00+01:00",
        "end": "2025-01-02T12:00:00Z",
    }
    out = await backend.call(name, args)
    assert len(out["sessions"]["items"]) == 1
    params = recording_transport.calls[0].url.params
    expected = {
        "mac_address": NORMALIZED,
        "username": "user1",
        "nasipaddress": "192.0.2.1",
        "servicetype": "Login-User",
    }
    if name == "clearpass_list_auth_failures":
        expected["auth_status"] = "FAILED"
    assert json.loads(params["filter"]) == expected
    assert set(params) == {"offset", "limit", "calculate_count", "sort", "filter"}


async def test_time_filter_scans_and_continues_without_losing_matches(backend, recording_transport):
    rows = [session(id=str(i), acctstarttime=f"2025-01-0{5 - i}T12:00:00Z") for i in range(4)]

    def paged(request):
        offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
        return {"_embedded": {"items": rows[offset : offset + limit]}}

    recording_transport.reply(paged, "GET", "/api/session")
    args = {"username": "user1", "limit": 2, "end": "2025-01-04T12:00:00Z"}
    out = await backend.call("clearpass_why_denied", args)
    assert [row["id"] for row in out["sessions"]] == ["1", "2"]
    assert out["next_server_offset"] == 3
    out = await backend.call("clearpass_why_denied", {**args, "offset": out["next_server_offset"]})
    assert [row["id"] for row in out["sessions"]] == ["3"]
    assert "next_server_offset" not in out


@pytest.mark.parametrize("timestamp", [None, 42, "invalid"])
async def test_bounded_empty_scan_counts_bad_timestamps(backend, recording_transport, timestamp):
    reply_sessions(recording_transport, [session(acctstarttime=timestamp)])
    out = await backend.call(
        "clearpass_why_denied",
        {
            "username": "user1",
            "limit": 1,
            "start": "2025-01-01T00:00:00Z",
        },
    )
    assert out["sessions"] == [] and out["next_server_offset"] == 10
    assert out["time_window"]["unusable_timestamps"] == 10
    assert "More records remain" in out["message"]
    assert len(recording_transport.calls) == 10


async def test_lower_time_bound_and_legacy_status_filter(backend, recording_transport):
    reply_sessions(
        recording_transport,
        [
            session(id="newer", acctstarttime="2025-01-03T00:00:00Z"),
            session(id="boundary", acctstarttime="2025-01-02T00:00:00Z"),
            session(id="older", acctstarttime="2025-01-01T23:59:59Z"),
        ],
    )
    out = await backend.call(
        "clearpass_list_access_tracker_sessions",
        {
            "status": "REJECT",
            "start": "2025-01-02T00:00:00Z",
        },
    )
    assert [row["id"] for row in out["sessions"]["items"]] == ["newer", "boundary"]
    assert json.loads(recording_transport.calls[0].url.params["filter"]) == {"auth_status": "REJECT"}


async def test_limit_and_offset_are_clamped(backend, recording_transport):
    reply_sessions(recording_transport, [session(id=str(i)) for i in range(201)])
    out = await backend.call("clearpass_why_denied", {"username": "user1", "limit": 1000, "offset": -1})
    assert len(out["sessions"]) == 200 and out["next_server_offset"] == 200
    assert recording_transport.calls[0].url.params["limit"] == "200"
    assert recording_transport.calls[0].url.params["offset"] == "0"


async def test_username_endpoint_is_only_latest_mac(backend, recording_transport):
    reply_sessions(
        recording_transport,
        [
            session(mac_address=None, callingstationid=MAC),
            session(mac_address="020000000002"),
        ],
    )
    await backend.call("clearpass_why_denied", {"username": "user1"})
    assert [call.url.path for call in recording_transport.calls] == ["/api/session", ENDPOINT]


async def test_session_read_error_is_not_reported_as_empty(backend, recording_transport):
    recording_transport.reply((403, {"detail": "Forbidden"}), "GET", "/api/session")
    out = await backend.call("clearpass_why_denied", {"mac": MAC})
    assert "error" in out and "message" not in out
    assert len(recording_transport.calls) == 1


def test_registered_kind_schema_and_ranking(backend):
    assert labels("clearpass")["clearpass_why_denied"]["kind"] == "read"
    for name in ("clearpass_why_denied", "clearpass_list_access_tracker_sessions", "clearpass_list_auth_failures"):
        props = backend.schema(name)["properties"]
        for field in ("mac", "username", "nad", "service", "start", "end"):
            assert props[field]["description"]
    for question in (
        "why was this client denied on clearpass",
        "why did clearpass reject this username",
        "clearpass why denied mac authentication failure reason",
    ):
        assert find.find_tool(question, top_k=3)[0]["name"] == "clearpass_why_denied"


async def test_routed_summary_budget_and_cursor(server_with_replies):
    server_with_replies(
        {
            ("GET", "/api/session"): {
                "_embedded": {"items": [session(id=str(i), auth_error="x" * 1800) for i in range(20)]}
            },
            ("GET", ENDPOINT): {"status": "Known"},
        },
        read_only=True,
        environ={"CLEARPASS_BASE_URL": "https://192.0.2.2", "CLEARPASS_API_TOKEN": "synthetic-token"},
    )
    args = {"mac": MAC, "limit": 20}
    first = await dispatch.invoke_read_tool("clearpass_why_denied", args)
    assert len(json.dumps(first).encode()) <= RESPONSE_BUDGET_BYTES
    assert 0 < len(first["sessions"]) < 20
    assert first["next_cursor"]
    second = await dispatch.invoke_read_tool("clearpass_why_denied", args, cursor=first["next_cursor"])
    assert second["sessions"][0]["id"] == str(len(first["sessions"]))
