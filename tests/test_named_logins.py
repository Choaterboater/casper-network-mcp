"""Named Central logins: ``CENTRAL_<NAME>_*`` read at start, picked per call with ``login``."""

from __future__ import annotations

import httpx
import pytest
from conftest import CENTRAL_BASE

from casper_network_mcp.core.budget import encode_cursor
from casper_network_mcp.core.logins import read_central_logins

TEMP_BASE = "https://us5.api.central.arubanetworks.com"
ENV = {
    "CENTRAL_BASE_URL": CENTRAL_BASE,
    "CENTRAL_CLIENT_ID": "main-id",
    "CENTRAL_CLIENT_SECRET": "main-secret",
    "CENTRAL_TEMP_BASE_URL": TEMP_BASE,
    "CENTRAL_TEMP_CLIENT_ID": "temp-id",
    "CENTRAL_TEMP_CLIENT_SECRET": "temp-secret",
    "MIST_API_TOKEN": "test-token",
}


def test_named_logins_are_read_by_name():
    env = {
        **ENV,
        "CENTRAL_LAB2_CLIENT_ID": "lab-id",
        "CENTRAL_BLANK_CLIENT_ID": "  ",
        "CENTRAL_DEFAULT_CLIENT_ID": "ignored",
        "CENTRAL_lower_CLIENT_ID": "ignored",
        "CENTRAL_TWO_PARTS_CLIENT_ID": "ignored",
    }
    logins = read_central_logins(env)
    assert list(logins) == ["lab2", "temp"]
    assert logins["temp"] == {
        "CENTRAL_BASE_URL": TEMP_BASE,
        "CENTRAL_CLIENT_ID": "temp-id",
        "CENTRAL_CLIENT_SECRET": "temp-secret",
    }
    assert logins["lab2"] == {"CENTRAL_CLIENT_ID": "lab-id"}


@pytest.fixture
def server(server_with_replies):
    return server_with_replies({}, environ=ENV)


def _hosts(transport) -> set[str]:
    return {call.url.host for call in transport.calls}


@pytest.mark.parametrize("tool", ["list_sites", "central_read_app_bandwidth_contracts"])  # hand-written, generated
async def test_login_picks_which_central_account_answers(server, recording_transport, tool):
    await server.call("invoke_read_tool", {"name": tool, "arguments": {}, "login": "temp"})
    assert _hosts(recording_transport) == {"us5.api.central.arubanetworks.com"}
    assert {httpx.QueryParams(c.content.decode()).get("client_id") for c in recording_transport.token_calls} == {
        "temp-id"
    }

    recording_transport.calls.clear()
    await server.call("invoke_read_tool", {"name": tool, "arguments": {}})
    assert _hosts(recording_transport) == {"de1.api.central.arubanetworks.com"}


async def test_async_troubleshooting_tool_keeps_the_login(server, recording_transport, monkeypatch):
    from casper_network_mcp.products.central import compat

    monkeypatch.setattr(compat, "POLL_INTERVAL", 0)
    path = "/network-troubleshooting/v1/aps/SER1/nslookup"
    recording_transport.reply(httpx.Response(202, headers={"Location": "/x/async-operations/t1"}), "POST", path)
    recording_transport.reply({"status": "COMPLETED"}, "GET", f"{path}/async-operations/t1")
    out = await server.call(
        "invoke_read_tool",
        {"name": "ap_nslookup", "arguments": {"serial_number": "SER1", "host": "example.com"}, "login": "temp"},
    )
    assert out["status"] == "COMPLETED"
    assert _hosts(recording_transport) == {"us5.api.central.arubanetworks.com"}
    assert len(recording_transport.calls) == 2


async def test_unknown_login_is_refused_before_anything_is_sent(server, recording_transport):
    out = await server.call("invoke_read_tool", {"name": "list_sites", "arguments": {}, "login": "nope"})
    assert out["status"] == "unknown_login" and "default, temp" in out["error"]
    out = await server.call("invoke_tool", {"name": "list_sites", "arguments": {}, "login": "nope"})
    assert out["status"] == "unknown_login"
    assert recording_transport.calls == []


async def test_login_is_only_for_central_tools(server, recording_transport):
    out = await server.call(
        "invoke_read_tool", {"name": "mist_list_sites", "arguments": {"org_id": "o1"}, "login": "temp"}
    )
    assert out["status"] == "login_not_for_product"
    assert recording_transport.calls == []


async def test_default_can_be_named(server, recording_transport):
    await server.call("invoke_read_tool", {"name": "list_sites", "arguments": {}, "login": "default"})
    assert _hosts(recording_transport) == {"de1.api.central.arubanetworks.com"}


async def test_a_cursor_never_resumes_under_another_login(server):
    args = {"limit": 10}
    temp_cursor = encode_cursor(name="list_sites", arguments={"arguments": args, "login": "temp"}, next_offset=5)
    out = await server.call("invoke_read_tool", {"name": "list_sites", "arguments": args, "cursor": temp_cursor})
    assert out["status"] == "invalid_cursor"
    plain_cursor = encode_cursor(name="list_sites", arguments=args, next_offset=5)
    out = await server.call(
        "invoke_read_tool", {"name": "list_sites", "arguments": args, "cursor": plain_cursor, "login": "temp"}
    )
    assert out["status"] == "invalid_cursor"


async def test_access_check_lists_named_logins(server_with_replies):
    server = server_with_replies({}, environ={**ENV, "CENTRAL_HALF_CLIENT_ID": "x"})
    out = await server.call("access_check", {})
    central = next(p for p in out["products"] if p["product"] == "central")
    assert central["named_logins"] == [{"name": "half", "login": "missing"}, {"name": "temp", "login": "set"}]


async def test_no_named_logins_leaves_access_check_as_it_was(server_with_replies):
    server = server_with_replies({}, environ={k: v for k, v in ENV.items() if "_TEMP_" not in k})
    out = await server.call("access_check", {})
    central = next(p for p in out["products"] if p["product"] == "central")
    assert "named_logins" not in central
