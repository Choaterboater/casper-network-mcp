"""The router's surface: four top-level tools, inner kinds visible, reads kept to reads.

The plan's Task 11 tests. Annotations are read with the SDK's own field names
(``destructive_hint``); mcp 2.x has no camelCase attributes.
"""

from __future__ import annotations

from casper_network_mcp.server import build_server


async def test_only_four_top_level_tools():
    server = build_server(read_only=True)
    names = sorted(t.name for t in await server.list_tools())
    assert names == ["access_check", "find_tool", "invoke_read_tool", "invoke_tool"]


async def test_find_tool_reports_inner_kind():
    server = build_server(read_only=True)
    hits = await server.call("find_tool", {"query": "reboot an access point", "top_k": 3})
    assert any(h["kind"] == "disruptive" for h in hits)


async def test_invoke_read_tool_refuses_a_write():
    server = build_server(read_only=False)
    out = await server.call("invoke_read_tool", {"name": "mist_update_wlan", "arguments": {}})
    assert out["error"] == "not_a_read_tool"


async def test_router_annotations():
    tools = {t.name: t for t in await build_server(read_only=True).list_tools()}
    assert tools["invoke_tool"].annotations.destructive_hint is False
    assert tools["invoke_tool"].annotations.read_only_hint is False
    assert tools["invoke_read_tool"].annotations.read_only_hint is True
    assert tools["find_tool"].annotations.read_only_hint is True
    assert tools["access_check"].annotations.read_only_hint is True


async def test_login_missing_is_structured(monkeypatch):
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    server = build_server(read_only=True)
    out = await server.call("invoke_read_tool", {"name": "mist_list_sites", "arguments": {"org_id": "o1"}})
    assert out == {"error": "login_missing", "product": "mist"}


async def test_find_tool_hit_shape():
    server = build_server(read_only=True)
    hits = await server.call("find_tool", {"query": "list mist sites", "top_k": 3})
    assert hits and set(hits[0]) == {"name", "product", "summary", "kind", "label"}
    with_schema = await server.call("find_tool", {"query": "list mist sites", "top_k": 1, "include_schema": True})
    assert "properties" in with_schema[0]["schema"]


async def test_lookup_api_is_found_through_find_tool():
    server = build_server(read_only=True)
    hits = await server.call("find_tool", {"query": "lookup api spec endpoint", "top_k": 5})
    assert "lookup_api" in [h["name"] for h in hits]
    out = await server.call("invoke_read_tool", {"name": "lookup_api", "arguments": {"query": "wlans", "top_k": 1}})
    assert isinstance(out, list) and out


async def test_find_tool_product_filter():
    server = build_server(read_only=True)
    hits = await server.call("find_tool", {"query": "list sites", "top_k": 5, "product": "clearpass"})
    assert all(h["product"] == "clearpass" for h in hits)


async def test_exact_method_and_path_finds_the_generated_tool():
    server = build_server(read_only=True)
    hits = await server.call("find_tool", {"query": "GET /api/v1/sites/{site_id}/wlans", "top_k": 1})
    assert hits[0]["product"] == "mist" and hits[0]["kind"] == "read"


async def test_unknown_tool():
    out = await build_server(read_only=True).call("invoke_tool", {"name": "no_such_tool", "arguments": {}})
    assert out["status"] == "unknown_tool"


async def test_generated_write_post_is_not_a_read_tool():
    from conftest import first_write_tool

    server = build_server(read_only=False)
    out = await server.call("invoke_read_tool", {"name": first_write_tool("mist"), "arguments": {}})
    assert out["error"] == "not_a_read_tool"


async def test_troubleshoot_runs_through_invoke_read_tool(monkeypatch):
    server = build_server(read_only=True)
    hits = await server.call("find_tool", {"query": "cx ping", "top_k": 5, "product": "central"})
    assert "cx_ping" in [h["name"] for h in hits]
    out = await server.call(
        "invoke_read_tool", {"name": "cx_ping", "arguments": {"serial_number": "SN1", "destination": "192.0.2.1"}}
    )
    assert out.get("error") != "not_a_read_tool"
