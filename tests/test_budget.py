import json

from casper_network_mcp.core.budget import (
    MAX_LIST_LIMIT,
    bound_collection_response,
    bounded_response_payload,
    clamp_limit,
)


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


class _Reply:
    def __init__(self, body: bytes, content_type: str) -> None:
        self.content = body
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.content)


def test_a_reply_too_big_to_return_whole_is_redacted_before_it_is_cut():
    item = {"name": "Test Site", "auth": {"type": "psk", "psk": "psk-value-0001"}, "pad": "x" * 4000}
    out = bounded_response_payload(_Reply(json.dumps({"wlans": [item] * 100}).encode(), "application/json"))
    assert out["truncated"] is True and "[hidden]" in out["text"]
    assert "psk-value-0001" not in out["text"]


def test_text_that_is_not_one_json_value_is_redacted():
    body = b'{"name": "Test Site", "secret": "radius-value-0001"}\n{"name": "Test Site 2"}\n'
    out = bounded_response_payload(_Reply(body, "application/json"))
    assert "radius-value-0001" not in out["text"] and "Test Site 2" in out["text"]
