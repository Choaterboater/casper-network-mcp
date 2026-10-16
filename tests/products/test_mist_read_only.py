"""Mist API reads remain usable while changes are refused before HTTP.

All automated calls use MockTransport and synthetic IDs, never real logins.
This covers the generated operations and curated tools, not live endpoint behavior.
Listed troubleshooting POSTs are deliberately excluded: the server permits them
under --read-only. Cable tests are disruptive changes and are covered below.
Live validation must use plain reads, not every tool accepted by invoke_read_tool.
"""

from __future__ import annotations

import re

import pytest
from conftest import placeholder_args
from spec_check import all_hand_written_tools, calls_for, match

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.gate import Gate
from casper_network_mcp.core.kinds import kind_for_operation, labels
from casper_network_mcp.openapi_gen.manifest import load_manifest
from casper_network_mcp.openapi_gen.runtime import generated_backend
from casper_network_mcp.products import _tools
from casper_network_mcp.specs_bundle import operations

MANIFEST = load_manifest("mist")
DOCUMENTED = {(op.method, op.path): op for op in operations("mist")}


def _kind(op):
    return kind_for_operation(op["method"], MANIFEST.base_path + op["path"], op.get("operation_id", ""))


GENERATED_READS = [op for op in MANIFEST.operations if _kind(op) == "read"]
GENERATED_CHANGES = [op for op in MANIFEST.operations if _kind(op) not in {"read", "troubleshoot"}]
CURATED = [tool for tool in all_hand_written_tools() if tool.product == "mist"]
CURATED_READS = [tool for tool in CURATED if labels("mist")[tool.name]["kind"] == "read" and tool.name != "mist_get"]
CURATED_CHANGES = [tool for tool in CURATED if labels("mist")[tool.name]["kind"] != "read"]


@pytest.fixture(scope="module")
def generated_mist():
    # Register once, but resolve the function-scoped fake client at call time.
    return generated_backend("mist", client=lambda: _tools.client("mist"))


@pytest.fixture
async def read_only_mist(monkeypatch, recording_transport, mist_client_factory):
    client = mist_client_factory(gate=Gate(read_only=True), transport=recording_transport)
    monkeypatch.setitem(_tools._getters, "mist", lambda: client)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.parametrize("op", GENERATED_READS, ids=lambda op: op["name"])
async def test_generated_mist_read_reaches_its_documented_api(op, generated_mist, read_only_mist, recording_transport):
    tool = sdk_compat.get_tool(generated_mist, op["name"])
    out = await sdk_compat.call_tool_raw(generated_mist, op["name"], placeholder_args(tool))

    assert out == {"ok": True}
    assert len(recording_transport.calls) == 1
    request = recording_transport.calls[0]
    assert request.method == op["method"] == "GET"
    assert request.content == b""
    documented = DOCUMENTED[(op["method"], op["path"])]
    # Match this operation's template directly: /{zone_type}/count overlaps
    # /rssizones/{rssizone_id}, which the shared prefix-first matcher prefers.
    pattern = "/".join(
        "[^/]+" if segment.startswith("{") and segment.endswith("}") else re.escape(segment)
        for segment in documented.path.split("/")
    )
    assert re.fullmatch(pattern, request.url.path)
    assert documented.operation_id == op["operation_id"]
    assert set(request.url.params) <= set(documented.query_params)
    assert set(documented.required_query) <= set(request.url.params)


@pytest.mark.parametrize("op", GENERATED_CHANGES, ids=lambda op: op["name"])
async def test_generated_mist_change_is_blocked_before_http(op, generated_mist, read_only_mist, recording_transport):
    tool = sdk_compat.get_tool(generated_mist, op["name"])
    out = await sdk_compat.call_tool_raw(generated_mist, op["name"], placeholder_args(tool))

    assert "read-only" in out["error"]
    assert recording_transport.calls == []
    assert recording_transport.token_calls == []


@pytest.mark.parametrize("tool", CURATED_READS, ids=lambda tool: tool.name)
async def test_curated_mist_read_sends_only_documented_gets(tool, read_only_mist, recording_transport):
    for args in calls_for(tool):
        recording_transport.calls.clear()
        out = await sdk_compat.call_tool_raw(tool.server, tool.name, args)
        # calls_for also produces combinations the tools intentionally reject.
        if tool.name in {"site_events", "org_events"} and args.get("mac") and args.get("kind", "system") != "devices":
            assert "mac narrows device events only" in out["error"]
            assert recording_transport.calls == []
            continue
        if (
            tool.name in {"sle_metrics", "sle_summary"}
            and args.get("scope", "site") != "site"
            and not args.get("scope_id")
        ):
            assert "scope_id is required" in out["error"]
            assert recording_transport.calls == []
            continue
        assert recording_transport.calls, (tool.name, args)
        for request in recording_transport.calls:
            assert request.method == "GET"
            assert request.content == b""
            assert match(request.method, request.url.path, "mist") is not None


@pytest.mark.parametrize("tool", CURATED_CHANGES, ids=lambda tool: tool.name)
async def test_curated_mist_change_is_blocked_before_http(tool, read_only_mist, recording_transport):
    out = await sdk_compat.call_tool_raw(tool.server, tool.name, calls_for(tool)[0])

    assert "read-only" in out["error"]
    assert recording_transport.calls == []
    assert recording_transport.token_calls == []


@pytest.mark.parametrize("name", ["mist_list_sites", "mist_list_org_sites", "mist_get"])
async def test_read_only_router_returns_api_data(name, server_with_replies, recording_transport):
    path = "/api/v1/orgs/o1/sites"
    server = server_with_replies({("GET", path): [{"id": "s1", "name": "Test site"}]}, read_only=True)
    args = {"path": path} if name == "mist_get" else {"org_id": "o1"}

    out = await server.call("invoke_read_tool", {"name": name, "arguments": args})

    collection = out["sites"] if name == "mist_list_sites" else out
    assert collection["items"] == [{"id": "s1", "name": "Test site"}]
    assert [(request.method, request.url.path) for request in recording_transport.calls] == [("GET", path)]


@pytest.mark.parametrize("entrypoint", ["invoke_read_tool", "invoke_tool"])
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("mist_update_wlan", {"site_id": "s1", "wlan_id": "w1", "changes": {"vlan_id": 30}}),
        ("mist_update_site_wlan", {"site_id": "s1", "wlan_id": "w1", "body": {"vlan_id": 30}}),
        ("mist_delete_wlan", {"site_id": "s1", "wlan_id": "w1"}),
        ("mist_delete_site_wlan", {"site_id": "s1", "wlan_id": "w1"}),
    ],
)
async def test_router_cannot_send_mist_changes(entrypoint, name, args, server_with_replies, recording_transport):
    server = server_with_replies({}, read_only=True)
    out = await server.call(entrypoint, {"name": name, "arguments": args})

    if entrypoint == "invoke_read_tool":
        assert out["error"] == "not_a_read_tool"
    else:
        assert "read-only" in out["error"]
    assert recording_transport.calls == []
    assert recording_transport.token_calls == []


async def test_raw_get_cannot_start_installer_optimization(server_with_replies, recording_transport):
    server = server_with_replies({}, read_only=True)
    out = await server.call(
        "invoke_read_tool", {"name": "mist_get", "arguments": {"path": "/api/v1/installer/sites/Test/optimize"}}
    )

    assert "changes" in out["error"]
    assert recording_transport.calls == []
