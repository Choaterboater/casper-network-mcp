"""invoke_read_tool shaping: all_pages joins every page, fields cuts items, group_by counts them."""

from __future__ import annotations

import pytest
from conftest import CENTRAL_BASE

from casper_network_mcp.router import summarize

INVENTORY = "/network-monitoring/v1/device-inventory"
CONTRACTS = "/network-config/v1alpha1/app-bandwidth-contracts"
ENV = {
    "CENTRAL_BASE_URL": CENTRAL_BASE,
    "CENTRAL_CLIENT_ID": "id",
    "CENTRAL_CLIENT_SECRET": "secret",
    "MIST_API_TOKEN": "test-token",
}


def _aps(n: int) -> list[dict]:
    return [
        {"serialNumber": f"SN{i}", "status": "ONLINE" if i % 3 else "OFFLINE", "radio": {"channel": 36 + i % 2}}
        for i in range(n)
    ]


def _token_pages(devices: list[dict], size: int):
    def answer(request):
        start = int(request.url.params.get("next") or 0)
        limit = int(request.url.params.get("limit") or size)
        page = devices[start : start + min(limit, size)]
        nxt = start + len(page)
        return {"items": page, "total": len(devices), "next": str(nxt) if nxt < len(devices) else None}

    return answer


def _offset_pages(devices: list[dict], size: int):
    def answer(request):
        start = int(request.url.params.get("offset") or 0)
        return {"items": devices[start : start + size], "total": len(devices)}

    return answer


@pytest.fixture
def server(server_with_replies):
    return server_with_replies({}, environ=ENV)


async def test_all_pages_follows_a_next_token_and_counts_by_field(server, recording_transport):
    recording_transport.reply(_token_pages(_aps(329), 100), "GET", INVENTORY)
    out = await server.call(
        "invoke_read_tool",
        {"name": "central_get_device_inventory_v1", "arguments": {}, "all_pages": True, "group_by": "status"},
    )
    assert out["counts"] == {"ONLINE": 219, "OFFLINE": 110} and out["total"] == 329
    assert out["_pages"] == {"fetched": 4, "items": 329, "complete": True, "stopped": "end of list"}
    assert len(recording_transport.calls) == 4


async def test_all_pages_follows_offsets_and_fields_cut_each_item(server, recording_transport):
    recording_transport.reply(_offset_pages(_aps(250), 200), "GET", CONTRACTS)
    out = await server.call(
        "invoke_read_tool",
        {
            "name": "central_read_app_bandwidth_contracts",
            "arguments": {},
            "all_pages": True,
            "fields": ["serialNumber", "radio.channel"],
        },
    )
    offsets = [c.url.params.get("offset") for c in recording_transport.calls]
    assert offsets == [None, "200"]
    assert out["_pages"]["items"] == 250 and out["_pages"]["complete"] is True
    first = out["items"][0]
    assert first == {"serialNumber": "SN0", "radio.channel": 36}


async def test_group_by_nested_field_on_one_page(server, recording_transport):
    recording_transport.reply({"items": _aps(10), "total": 10}, "GET", CONTRACTS)
    out = await server.call(
        "invoke_read_tool",
        {"name": "central_read_app_bandwidth_contracts", "arguments": {}, "group_by": "radio.channel"},
    )
    assert out["counts"] == {36: 5, 37: 5} and "items" not in out and "_pages" not in out


async def test_shaping_is_only_for_reads_and_checks_its_options(server, recording_transport):
    out = await server.call("invoke_read_tool", {"name": "list_sites", "arguments": {}, "fields": []})
    assert out["status"] == "bad_shaping"
    out = await server.call("invoke_read_tool", {"name": "list_sites", "arguments": {}, "group_by": " "})
    assert out["status"] == "bad_shaping"
    assert recording_transport.calls == []


async def test_a_page_that_fails_stops_and_says_where_to_resume():
    pages = {None: {"items": [1, 2], "next": "b"}, "b": {"error": "boom"}}

    async def call(args):
        return pages[args.get("next")]

    out = await summarize.collect_pages(call, {}, {"next", "limit"})
    assert out["items"] == [1, 2]
    assert out["_pages"]["complete"] is False and "boom" in out["_pages"]["stopped"]
    assert out["_pages"]["resume_arguments"] == {"next": "b"}


async def test_a_tool_that_ignores_paging_is_not_fetched_fifty_times():
    calls = []

    async def call(args):
        calls.append(args)
        return {"items": list(range(200))}

    out = await summarize.collect_pages(call, {}, {"offset", "limit"})
    assert len(calls) == 2 and out["_pages"]["items"] == 200
    assert "same page" in out["_pages"]["stopped"]


async def test_page_caps_stop_with_resume_arguments(monkeypatch):
    monkeypatch.setattr(summarize, "MAX_PAGES", 3)

    async def call(args):
        start = int(args.get("offset") or 0)
        return {"items": list(range(start, start + args["limit"]))}

    out = await summarize.collect_pages(call, {"limit": 10}, {"offset", "limit"})
    assert out["_pages"]["fetched"] == 3 and out["_pages"]["complete"] is False
    assert out["_pages"]["resume_arguments"] == {"offset": 30}


async def test_mist_page_numbers_stop_on_a_short_page():
    async def call(args):
        page = int(args.get("page") or 1)
        return [{"id": f"{page}-{i}"} for i in range(100 if page < 3 else 7)]

    out = await summarize.collect_pages(call, {"limit": 100}, {"page", "limit"})
    assert out["_pages"]["items"] == 207 and out["_pages"]["fetched"] == 3


async def test_unfollowable_paging_is_named_not_guessed():
    async def call(args):
        return {"results": [1, 2, 3], "next": "/api/v1/...search_after=x"}

    out = await summarize.collect_pages(call, {}, {"search_after", "limit"})
    assert out["_pages"]["complete"] is False and "search_after" in out["_pages"]["stopped"]


def test_group_by_lists_the_top_values_and_sums_the_rest():
    out = summarize.group_items([{"v": i} for i in range(150)], "v")
    assert out["distinct"] == 150 and len(out["counts"]) == summarize.MAX_GROUPS + 1
    assert out["counts"]["_other"] == 50


async def test_a_page_cut_short_is_never_followed_by_its_token():
    async def call(args):
        return {"items": [1, 2], "next": "b", "_pagination": {"truncated": True, "total": 5}}

    out = await summarize.collect_pages(call, {}, {"next", "limit"})
    assert out["_pages"]["complete"] is False and "cut short" in out["_pages"]["stopped"]


async def test_generated_tool_keeps_the_page_size_it_asked_for(server, recording_transport):
    recording_transport.reply({"items": _aps(150), "total": 150}, "GET", CONTRACTS)
    out = await server.call(
        "invoke_read_tool",
        {"name": "central_read_app_bandwidth_contracts", "arguments": {"limit": 150}, "fields": ["serialNumber"]},
    )
    assert out["_pagination"]["limit"] == 150 and out["_pagination"]["truncated"] is False


async def test_an_api_that_caps_its_page_lower_is_asked_again_at_its_default():
    calls = []

    async def call(args):
        calls.append(dict(args))
        if args.get("limit", 0) > 100:
            return {"error": "limit must be at most 100"}
        start = int(args.get("offset") or 0)
        return {"items": list(range(start, min(start + 100, 230)))}

    out = await summarize.collect_pages(call, {}, {"offset", "limit"})
    assert calls[0] == {"limit": 200} and calls[1] == {}
    assert out["_pages"]["items"] == 230 and out["_pages"]["complete"] is True


async def test_a_limit_the_caller_chose_is_never_second_guessed():
    async def call(args):
        return {"error": "limit must be at most 100"}

    out = await summarize.collect_pages(call, {"limit": 500}, {"offset", "limit"})
    assert out == {"error": "limit must be at most 100"}
