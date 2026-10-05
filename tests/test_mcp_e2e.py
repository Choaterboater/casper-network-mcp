"""The router over the real MCP protocol (in-memory client, real JSON-RPC framing).

The harness idea comes from hpe-networking-mcp ``tests/unit/test_mcp_protocol_e2e.py``; the server under test is this one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from mcp.client import Client

from casper_network_mcp.server import build_server


def _no_logins_env() -> dict[str, str]:
    """An empty environment. Windows also needs SYSTEMROOT to start Python's socket layer; it holds no login."""
    return {"SYSTEMROOT": os.environ["SYSTEMROOT"]} if sys.platform == "win32" else {}


def _payload(result):
    assert result.content, "expected a content block"
    return json.loads(result.content[0].text)


async def test_list_tools_over_the_wire():
    async with Client(build_server(read_only=True)) as client:
        listed = await client.list_tools()
    by_name = {t.name: t for t in listed.tools}
    assert sorted(by_name) == ["access_check", "find_tool", "invoke_read_tool", "invoke_tool"]
    assert by_name["invoke_read_tool"].annotations.read_only_hint is True
    assert by_name["invoke_tool"].annotations.destructive_hint is False


async def test_find_then_read_over_the_wire():
    async with Client(build_server(read_only=True)) as client:
        found = await client.call_tool("find_tool", {"query": "list mist sites", "top_k": 3})
        hits = found.structured_content["result"]
        assert hits[0]["name"] == "mist_list_sites"
        assert any(h["product"] == "mist" for h in hits)
        out = await client.call_tool("invoke_read_tool", {"name": "mist_list_sites", "arguments": {"org_id": "o1"}})
    assert _payload(out) == {"error": "login_missing", "product": "mist"}


async def test_read_only_pin_refuses_a_change_over_the_wire(recording_transport):
    server = build_server(read_only=True, transport=recording_transport, environ={"MIST_API_TOKEN": "t"})
    async with Client(server) as client:
        out = await client.call_tool(
            "invoke_tool",
            {"name": "mist_update_wlan", "arguments": {"site_id": "s1", "wlan_id": "w1", "changes": {"vlan_id": 30}}},
        )
    assert "read-only" in _payload(out)["error"]
    assert recording_transport.calls == []


async def test_unknown_router_tool_is_a_protocol_error():
    async with Client(build_server(read_only=True)) as client:
        out = await client.call_tool("no_such_tool", {})
    assert out.is_error


def test_help_runs_without_logins():
    proc = subprocess.run(
        [sys.executable, "-m", "casper_network_mcp", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_no_logins_env(),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--read-only" in proc.stdout


def test_building_the_server_loads_no_backend():
    code = (
        "from casper_network_mcp.server import build_server\n"
        "from casper_network_mcp.router import index\n"
        "build_server(read_only=True)\n"
        "print(index._cached.cache_info().currsize)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=_no_logins_env(), check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0"


def test_the_read_only_flag_reaches_the_gate(monkeypatch):
    from casper_network_mcp import server as server_module

    started = []
    monkeypatch.setattr(server_module, "run_server", lambda server, **kw: started.append((server, kw)))
    server_module.main(["--read-only"])
    server_module.main([])
    assert [s.gate.read_only for s, _ in started] == [True, False]
    assert started[0][1]["transport"] == "stdio"


async def test_the_read_only_flag_refuses_a_change_over_stdio():
    from mcp.client.stdio import StdioServerParameters

    # A Mist login that points at a documentation address: the pin refuses before any request.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "casper_network_mcp", "--read-only"],
        env={"MIST_API_TOKEN": "t", "MIST_HOST": "api.example.invalid"},
    )
    async with Client(params) as client:
        out = await client.call_tool(
            "invoke_tool",
            {"name": "mist_update_wlan", "arguments": {"site_id": "s1", "wlan_id": "w1", "changes": {"vlan_id": 30}}},
        )
    assert "read-only" in _payload(out)["error"]
