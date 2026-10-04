"""Signed continuation cursors for cut-short reads.

Ported from hpe-networking-mcp ``tests/unit/test_tool_router_cursor.py``. The cursor helpers now live in
``core/budget.py``; ``_read`` below stands in for the router's resume step
(re-run the same read, re-slice from the cursor's offset). Capability checks
(only read tools get cursors) move to the router tests.
"""

import base64
import json
import time

import pytest

from casper_network_mcp.core import budget
from casper_network_mcp.core.budget import (
    CURSOR_DIGEST_HEX_CHARS,
    CURSOR_MAX_LENGTH,
    MAX_LIST_LIMIT,
    CursorError,
    bound_router_response,
    decode_cursor,
    encode_cursor,
)


def _list_items(count=250, filter=None):
    return list(range(count))


def _list_devices(count=250):
    return {"devices": [{"serial": f"sn-{i}"} for i in range(count)], "meta": "ok"}


TOOLS = {"list_items": _list_items, "list_devices": _list_devices}


def _read(name, arguments, cursor=None, **budget_kwargs):
    offset = 0
    if cursor is not None:
        try:
            offset = decode_cursor(cursor, name=name, arguments=arguments)
        except CursorError as exc:
            return {"error": str(exc), "status": "invalid_cursor"}
    result = TOOLS[name](**arguments)
    return bound_router_response(
        result, offset=offset, enable_cursor=True, tool_name=name, tool_arguments=arguments, **budget_kwargs
    )


def test_two_plus_pages_no_overlap_or_gaps():
    collected = []
    out = _read("list_items", {"count": 100}, max_items=40)
    assert out["_response_bounds"]["truncated"] is True
    collected.extend(out["items"])
    pages = 1
    while "next_cursor" in out:
        out = _read("list_items", {"count": 100}, cursor=out["next_cursor"], max_items=40)
        assert "error" not in out
        collected.extend(out["items"])
        pages += 1
        assert pages < 20
    assert pages >= 3
    assert collected == list(range(100))


def test_nested_primary_list_dict_pagination():
    collected = []
    out = _read("list_devices", {"count": 90}, max_items=30)
    collected.extend(out["devices"])
    while "next_cursor" in out:
        out = _read("list_devices", {"count": 90}, cursor=out["next_cursor"], max_items=30)
        collected.extend(out["devices"])
    assert collected == [{"serial": f"sn-{i}"} for i in range(90)]
    assert out["meta"] == "ok"


def test_default_200_item_clamp_respected_across_pages():
    out = _read("list_items", {"count": 250})
    assert len(out["items"]) == MAX_LIST_LIMIT
    out2 = _read("list_items", {"count": 250}, cursor=out["next_cursor"])
    assert out2["items"] == list(range(MAX_LIST_LIMIT, 250))
    assert "next_cursor" not in out2
    assert "_response_bounds" not in out2


def test_byte_shrunk_page_next_offset_is_the_actual_page_size():
    out = _read("list_devices", {"count": 60}, max_items=50, max_bytes=400)
    first = len(out["devices"])
    assert 0 < first < 50
    out2 = _read("list_devices", {"count": 60}, cursor=out["next_cursor"], max_items=50, max_bytes=400)
    assert out2["_pagination"]["offset"] == first
    assert out2["devices"] == [{"serial": f"sn-{i}"} for i in range(first, first + len(out2["devices"]))]


def _first_cursor():
    out = _read("list_items", {"count": 100}, max_items=10)
    return out["next_cursor"]


def test_tampered_cursor_rejected():
    payload_part, sig_part = _first_cursor().split(".")
    out = _read("list_items", {"count": 100}, cursor=payload_part + "x." + sig_part)
    assert out["status"] == "invalid_cursor"


def test_flipped_signature_rejected():
    payload_part, sig_part = _first_cursor().split(".")
    flipped = ("A" if sig_part[0] != "A" else "B") + sig_part[1:]
    assert _read("list_items", {"count": 100}, cursor=f"{payload_part}.{flipped}")["status"] == "invalid_cursor"


def test_expired_cursor_rejected():
    cursor = encode_cursor(name="list_items", arguments={"count": 100}, next_offset=10, ttl_seconds=1)
    time.sleep(1.2)
    out = _read("list_items", {"count": 100}, cursor=cursor)
    assert out["status"] == "invalid_cursor" and "expired" in out["error"]


def test_wrong_tool_name_cursor_rejected():
    assert _read("list_devices", {"count": 100}, cursor=_first_cursor())["status"] == "invalid_cursor"


def test_changed_arguments_cursor_rejected():
    assert _read("list_items", {"count": 999}, cursor=_first_cursor())["status"] == "invalid_cursor"


def test_restart_key_mismatch_rejected(monkeypatch):
    cursor = _first_cursor()
    monkeypatch.setattr(budget, "_CURSOR_HMAC_KEY", b"\x01" * 32)
    out = _read("list_items", {"count": 100}, cursor=cursor)
    assert out["status"] == "invalid_cursor" and "restart" in out["error"]


def test_oversized_and_malformed_cursors_rejected():
    assert _read("list_items", {"count": 100}, cursor="a" * (CURSOR_MAX_LENGTH + 1))["status"] == "invalid_cursor"
    assert _read("list_items", {"count": 100}, cursor="not-a-real-cursor")["status"] == "invalid_cursor"


def test_no_cursor_without_enable_cursor():
    out = bound_router_response(list(range(100)), max_items=10)
    assert out["_response_bounds"]["truncated"] is True
    assert "next_cursor" not in out and out["_response_bounds"]["resumable"] is False


def test_no_router_cursor_when_backend_has_its_own():
    data = {"items": list(range(100)), "next_cursor": "backend-token"}
    out = bound_router_response(data, max_items=10, enable_cursor=True, tool_name="t", tool_arguments={})
    assert out["next_cursor"] == "backend-token"
    assert out["_response_bounds"]["resumable_reason"] == "upstream_cursor_present"


def test_decoded_cursor_payload_has_no_raw_arguments_or_secrets():
    secret = "TOP-SECRET-FILTER-VALUE-12345"
    out = _read("list_items", {"count": 100, "filter": secret}, max_items=10)
    payload_b64, _ = out["next_cursor"].split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    assert set(payload) == {"v", "exp", "off", "t", "a"}
    raw = json.dumps(payload)
    for word in (secret, "list_items", "count", "filter"):
        assert word not in raw
    assert len(payload["t"]) == CURSOR_DIGEST_HEX_CHARS and len(payload["a"]) == CURSOR_DIGEST_HEX_CHARS
    assert secret not in out["next_cursor"]


def test_huge_single_item_reports_non_resumable_with_no_cursor():
    out = bound_router_response(
        {"items": ["z" * 5000]}, max_bytes=1024, enable_cursor=True, tool_name="t", tool_arguments={}
    )
    assert "next_cursor" not in out
    assert out["_response_bounds"]["resumable"] is False
    assert "preview" in out


def test_round_trip_and_length():
    cursor = encode_cursor(name="some_tool", arguments={"a": 1}, next_offset=42)
    assert decode_cursor(cursor, name="some_tool", arguments={"a": 1}) == 42
    assert len(cursor) <= CURSOR_MAX_LENGTH
    with pytest.raises(CursorError):
        decode_cursor(cursor, name="some_tool", arguments={"a": 2})
    with pytest.raises(CursorError):
        decode_cursor("", name="t", arguments={})
    with pytest.raises(CursorError):
        decode_cursor("nodothere", name="t", arguments={})
