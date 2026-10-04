from casper_network_mcp.core.budget import MAX_LIST_LIMIT, bound_collection_response, clamp_limit


def test_list_is_wrapped_and_sliced():
    out = bound_collection_response(list(range(10)), limit=3)
    assert out["items"] == [0, 1, 2]
    assert out["_pagination"] == {"offset": 0, "limit": 3, "total": 10, "truncated": True}


def test_dict_slices_its_longest_list():
    out = bound_collection_response({"sites": [1, 2, 3, 4], "tags": [1], "org": "o"}, limit=2, offset=1)
    assert out["sites"] == [2, 3] and out["tags"] == [1] and out["org"] == "o"
    assert out["_pagination"]["list_key"] == "sites" and out["_pagination"]["truncated"] is True


def test_named_list_key():
    out = bound_collection_response({"a": [1, 2, 3], "b": [1]}, limit=5, list_key="b")
    assert out["_pagination"]["list_key"] == "b" and out["_pagination"]["truncated"] is False


def test_backend_cursor_is_kept():
    data = {"items": [1, 2, 3], "_pagination": {"next_cursor": "abc", "total": 3}}
    out = bound_collection_response(data, limit=2)
    assert out["_pagination"]["next_cursor"] == "abc"


def test_non_collections_pass_through():
    assert bound_collection_response("x", limit=1) == "x"
    data = {"a": 1}
    assert bound_collection_response(data, limit=1) is data


def test_clamp_limit():
    assert clamp_limit(None) == 50
    assert clamp_limit(0) == 1
    assert clamp_limit(10_000) == MAX_LIST_LIMIT
