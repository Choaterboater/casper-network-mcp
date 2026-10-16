"""Mist search paging: follow the API's own ``search_after`` cursor, report what is left.

All replies are synthetic recordings; nothing reaches a live API.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from casper_network_mcp.core.gate import Gate

CURSOR = '[1700000000000, "02000000000a"]'
PORTS = "/api/v1/sites/s1/stats/ports/search"


def _port(n: int) -> dict[str, object]:
    return {"mac": "020000000001", "port_id": f"ge-0/0/{n}", "up": True}


def _page(results: list[object], total: int, next_cursor: str | None, path: str) -> dict[str, object]:
    page: dict[str, object] = {"results": results, "total": total, "limit": len(results)}
    if next_cursor is not None:
        page["next"] = f"{path}?limit={len(results)}&search_after={quote(next_cursor)}"
    return page


def _params(request) -> dict[str, str]:
    return dict(request.url.params)


async def test_switch_ports_report_more_and_hand_back_the_cursor(recording_transport, mist_backend):
    recording_transport.reply(_page([_port(n) for n in range(2)], 5, CURSOR, PORTS), "GET", PORTS)
    out = await mist_backend.call(
        "mist_list_switch_ports", {"site_id": "s1", "switch_mac": "02:00:00:00:00:01", "limit": 2}
    )
    pagination = out["ports"]["_pagination"]
    assert pagination["truncated"] is True and pagination["total"] == 5
    assert pagination["search_after"] == CURSOR


async def test_switch_ports_continue_from_the_cursor(recording_transport, mist_backend):
    recording_transport.reply(_page([_port(n) for n in range(2, 5)], 5, None, PORTS), "GET", PORTS)
    out = await mist_backend.call(
        "mist_list_switch_ports",
        {"site_id": "s1", "switch_mac": "02:00:00:00:00:01", "limit": 3, "search_after": CURSOR},
    )
    assert _params(recording_transport.calls[0])["search_after"] == CURSOR
    assert [p["port_id"] for p in out["ports"]["items"]] == ["ge-0/0/2", "ge-0/0/3", "ge-0/0/4"]
    assert out["ports"]["_pagination"]["truncated"] is False


async def test_hand_written_search_takes_back_a_json_cursor(recording_transport, mist_backend):
    path = "/api/v1/sites/s1/alarms/search"
    recording_transport.reply(_page([], 0, None, path), "GET", path)
    out = await mist_backend.call("mist_list_alarms", {"site_id": "s1", "search_after": CURSOR})
    assert "error" not in out
    assert _params(recording_transport.calls[0])["search_after"] == CURSOR


@pytest.mark.parametrize("cursor", [CURSOR, '{"t": 1}', "[1]"])
async def test_generated_search_takes_back_a_json_cursor(cursor, recording_transport, generated_backend_factory):
    path = "/api/v1/sites/s1/alarms/search"
    recording_transport.reply(_page([], 0, None, path), "GET", path)
    backend = generated_backend_factory("mist", gate=Gate(read_only=True), transport=recording_transport)
    out = await backend.call("mist_search_site_alarms", {"site_id": "s1", "search_after": cursor})
    assert not (isinstance(out, dict) and "error" in out), out
    assert _params(recording_transport.calls[0])["search_after"] == cursor


async def test_nac_troubleshoot_asks_the_api_for_the_client(recording_transport, mist_backend):
    path = "/api/v1/sites/s1/nac_clients/search"
    recording_transport.reply(_page([{"mac": "020000000001", "username": "user1"}], 1, None, path), "GET", path)
    out = await mist_backend.call(
        "mist_nac_troubleshoot", {"org_id": "o1", "site_id": "s1", "mac": "02:00:00:00:00:01", "duration": "7d"}
    )
    search = next(c for c in recording_transport.calls if c.url.path == path)
    assert _params(search)["mac"] == "020000000001" and _params(search)["duration"] == "7d"
    assert out["clients"]["items"][0]["mac"] == "020000000001"


async def test_nac_troubleshoot_pages_to_find_the_client(recording_transport, mist_backend):
    # A search that ignores the filter still pages: the client is on page two.
    path = "/api/v1/sites/s1/nac_clients/search"
    first = _page([{"mac": "020000000002"}], 2, CURSOR, path)
    second = _page([{"mac": "020000000001"}], 2, None, path)
    recording_transport.reply(lambda r: second if "search_after" in r.url.params else first, "GET", path)
    out = await mist_backend.call("mist_nac_troubleshoot", {"org_id": "o1", "site_id": "s1", "mac": "020000000001"})
    assert [c["mac"] for c in out["clients"]["items"]] == ["020000000001"]
