"""Response budget for routed tool results.

Ported from hpe-networking-mcp ``tests/unit/test_tool_router_response_budget.py``
(MIT, nowireless4u/hpe-networking-mcp). The budget now lives in
``core/budget.py`` with fixed limits (no environment overrides); the
through-the-router cases move to the router tests.
"""

from casper_network_mcp.core import budget
from casper_network_mcp.core.budget import MAX_LIST_LIMIT, bound_router_response


def test_small_dict_passes_through_unchanged():
    result = {"a": 1, "b": [1, 2, 3]}
    assert bound_router_response(result, max_items=500, max_bytes=200_000) is result


def test_small_list_passes_through_unchanged():
    result = [1, 2, 3]
    assert bound_router_response(result, max_items=500, max_bytes=200_000) is result


def test_scalar_passes_through_unchanged():
    assert bound_router_response("hello", max_items=1, max_bytes=1) == "hello"
    assert bound_router_response(42, max_items=1, max_bytes=1) == 42
    assert bound_router_response(None, max_items=1, max_bytes=1) is None
    assert bound_router_response(True, max_items=1, max_bytes=1) is True


def test_error_dict_never_touched_even_when_huge():
    result = {"error": "boom", "detail": "x" * 5000}
    assert bound_router_response(result, max_items=1, max_bytes=10) is result


def test_oversized_list_gets_pagination_and_response_bounds():
    out = bound_router_response(list(range(100)), max_items=10, max_bytes=200_000)
    assert out["_pagination"]["truncated"] is True
    assert out["_response_bounds"]["truncated"] is True
    assert out["_response_bounds"]["reason"] == "item_budget"
    assert out["_response_bounds"]["item_limit"] == 10
    assert len(out["items"]) == 10


def test_requested_item_budget_is_capped_to_shared_collection_limit():
    out = bound_router_response({"items": [{"i": i} for i in range(600)]}, max_items=500, max_bytes=200_000)
    assert len(out["items"]) == MAX_LIST_LIMIT
    assert out["_pagination"]["limit"] == MAX_LIST_LIMIT
    assert out["_response_bounds"]["item_limit"] == MAX_LIST_LIMIT


def test_default_budget_is_fixed():
    assert budget.RESPONSE_BUDGET_ITEMS == MAX_LIST_LIMIT
    out = bound_router_response(list(range(MAX_LIST_LIMIT + 1)))
    assert len(out["items"]) == MAX_LIST_LIMIT


def test_dict_with_oversized_nested_list_gets_bounded():
    result = {"devices": [{"serial": f"sn-{i}"} for i in range(200)], "meta": "ok"}
    out = bound_router_response(result, max_items=5, max_bytes=200_000)
    assert len(out["devices"]) == 5
    assert out["_response_bounds"]["reason"] == "item_budget"
    assert out["meta"] == "ok"


def test_byte_budget_alone_triggers_item_shrink():
    out = bound_router_response({"items": [{"blob": "y" * 200} for _ in range(50)]}, max_items=500, max_bytes=2000)
    assert len(out["items"]) < 50
    assert "byte_budget" in out["_response_bounds"]["reason"]


def test_nothing_sliceable_falls_back_to_preview():
    out = bound_router_response({"summary": "z" * 5000}, max_items=500, max_bytes=1024)
    assert out["_response_bounds"]["reason"] == "byte_budget"
    assert isinstance(out["preview"], str)
    assert len(out["preview"].encode("utf-8")) <= 1024


def test_huge_single_item_falls_back_to_preview_when_slicing_cannot_help():
    out = bound_router_response({"items": ["z" * 5000]}, max_items=500, max_bytes=1024)
    assert "preview" in out
    assert out["_response_bounds"]["reason"] == "byte_budget"


def test_within_item_budget_but_not_byte_budget_reports_byte_reason():
    out = bound_router_response({"items": [{"blob": "y" * 500} for _ in range(3)]}, max_items=500, max_bytes=600)
    assert out["_response_bounds"]["reason"] in {"byte_budget", "item_budget+byte_budget"}
