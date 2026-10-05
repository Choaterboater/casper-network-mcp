"""Dispatch through the router: shapes, errors, bounds and cursors.

Ported from hpe-networking-mcp ``tests/unit/test_tool_router_dispatch.py``,
``test_router_error_redaction.py``, ``test_tool_router_cursor.py`` and
``test_tool_router_response_budget.py``,
over a small fake catalog. Cut: the per-platform write switches and the
``HPE_MCP_PRODUCT_ACCESS`` env cases (no env switches here), and the ctx
injection case (no inner tool takes a request context any more).
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.kinds import label_for_kind, tool_meta
from casper_network_mcp.router import dispatch, find
from casper_network_mcp.router.index import Catalog, _entry, index_of

_RAW_BEARER = "Bearer sk-abc1234567890-raise-path-secret-value"


def _backend() -> MCPServer:
    srv = MCPServer("test-backend")

    def add(fn, kind):
        srv.add_tool(fn, annotations=label_for_kind(kind), meta=tool_meta(kind))

    def sync_echo(value: int) -> dict[str, Any]:
        return {"kind": "sync", "value": value}

    async def async_echo(value: int) -> dict[str, Any]:
        return {"kind": "async", "value": value}

    def optional_limit(limit: int = 5) -> dict[str, Any]:
        return {"limit": limit}

    def boom() -> dict[str, Any]:
        raise RuntimeError("kaboom")

    def bearer_call() -> dict[str, Any]:
        raise RuntimeError(_RAW_BEARER)

    def bearer_prose_call() -> dict[str, Any]:
        raise RuntimeError("401 Unauthorized: Bearer token missing or expired")

    def write_echo(value: int) -> dict[str, Any]:
        return {"kind": "write", "value": value}

    def ping_check() -> dict[str, Any]:
        return {"ok": True}

    def list_items(count: int = 250) -> list[int]:
        return list(range(count))

    def list_devices(count: int = 250) -> dict[str, Any]:
        return {"devices": [{"serial": f"sn-{i}"} for i in range(count)], "meta": "ok"}

    def list_big_writes(count: int = 250) -> list[int]:
        return list(range(count))

    for fn in (sync_echo, async_echo, optional_limit, boom, bearer_call, bearer_prose_call, list_items, list_devices):
        add(fn, "read")
    add(write_echo, "config")
    add(ping_check, "troubleshoot")
    add(list_big_writes, "config")
    return srv


@pytest.fixture
def fake_catalog(monkeypatch):
    srv = _backend()
    entries = {name: _entry(name, "mist", srv, tool, "curated") for name, tool in sdk_compat.tool_registry(srv).items()}
    cat = Catalog(entries)
    monkeypatch.setattr(dispatch, "catalog", lambda: cat)
    monkeypatch.setattr(find, "catalog", lambda: cat)
    monkeypatch.setattr(find, "load_index", lambda: index_of(cat))
    return cat


async def test_sync_and_async_tools(fake_catalog):
    assert await dispatch.invoke_tool("sync_echo", {"value": 7}) == {"kind": "sync", "value": 7}
    assert await dispatch.invoke_tool("async_echo", {"value": 9}) == {"kind": "async", "value": 9}


async def test_read_dispatcher_runs_reads_and_checks(fake_catalog):
    assert await dispatch.invoke_read_tool("sync_echo", {"value": 7}) == {"kind": "sync", "value": 7}
    assert await dispatch.invoke_read_tool("ping_check", {}) == {"ok": True}


async def test_null_arguments_are_dropped(fake_catalog):
    assert await dispatch.invoke_read_tool("optional_limit", {"limit": None}) == {"limit": 5}


async def test_read_dispatcher_refuses_a_change(fake_catalog):
    out = await dispatch.invoke_read_tool("write_echo", {"value": 7})
    assert out["error"] == "not_a_read_tool" and out["kind"] == "config"


async def test_unknown_tool(fake_catalog):
    out = await dispatch.invoke_tool("does_not_exist", {})
    assert out["status"] == "unknown_tool" and "does_not_exist" in out["error"]


async def test_raised_error_is_a_plain_error(fake_catalog):
    out = await dispatch.invoke_tool("boom", {})
    assert "kaboom" in out["error"]
    out = await dispatch.invoke_tool("sync_echo", {})
    assert "error" in out


async def test_raised_bearer_credential_is_hidden(fake_catalog):
    out = await dispatch.invoke_read_tool("bearer_call", {})
    assert "sk-abc1234567890" not in repr(out)
    prose = await dispatch.invoke_read_tool("bearer_prose_call", {})
    assert "Bearer token missing or expired" in prose["error"]


async def test_big_read_is_cut_with_a_cursor_that_resumes(fake_catalog):
    first = await dispatch.invoke_read_tool("list_items", {})
    assert first["_response_bounds"]["resumable"] is True
    assert len(first["items"]) == 200
    rest = await dispatch.invoke_read_tool("list_items", {}, cursor=first["next_cursor"])
    assert rest["items"] == list(range(200, 250))


async def test_dict_collection_is_cut_by_its_list(fake_catalog):
    out = await dispatch.invoke_read_tool("list_devices", {})
    assert len(out["devices"]) == 200 and out["meta"] == "ok"
    rest = await dispatch.invoke_read_tool("list_devices", {}, cursor=out["next_cursor"])
    assert rest["devices"][0] == {"serial": "sn-200"}


async def test_cursor_is_bound_to_tool_and_arguments(fake_catalog):
    first = await dispatch.invoke_read_tool("list_items", {})
    other_args = await dispatch.invoke_read_tool("list_items", {"count": 300}, cursor=first["next_cursor"])
    assert other_args["status"] == "invalid_cursor"
    other_tool = await dispatch.invoke_read_tool("list_devices", {}, cursor=first["next_cursor"])
    assert other_tool["status"] == "invalid_cursor"
    junk = await dispatch.invoke_read_tool("list_items", {}, cursor="not-a-cursor")
    assert junk["status"] == "invalid_cursor"


async def test_a_change_is_bounded_but_never_gets_a_cursor(fake_catalog):
    out = await dispatch.invoke_tool("list_big_writes", {})
    assert out["_response_bounds"]["resumable"] is False
    assert "next_cursor" not in out


async def test_small_reply_is_unchanged(fake_catalog):
    assert await dispatch.invoke_read_tool("list_items", {"count": 3}) == [0, 1, 2]


async def test_find_tool_needs_a_name_word(fake_catalog):
    names = [h["name"] for h in find.find_tool("echo", top_k=10)]
    assert set(names) == {"sync_echo", "async_echo", "write_echo"}
    assert find.find_tool("zzz unrelated words") == []


async def test_bad_arguments_name_the_fields_in_plain_words(fake_catalog):
    missing = await dispatch.invoke_tool("sync_echo", {})
    assert missing["error"] == "sync_echo needs: value"
    wrong = await dispatch.invoke_tool("sync_echo", {"value": "not-a-number-secret"})
    assert wrong["error"] == "sync_echo has a wrong value for: value"
    assert "not-a-number-secret" not in repr(wrong) and "pydantic" not in repr(wrong)
