"""Generated tools tell the AI what each argument is: the spec's words, trimmed, or generic ones."""

from __future__ import annotations

import pytest

from casper_network_mcp import sdk_compat
from casper_network_mcp.openapi_gen.manifest import load_manifest
from casper_network_mcp.openapi_gen.runtime import (
    _FALLBACK_PARAM_DESCRIPTIONS,
    _MAX_BODY_FIELDS,
    _MAX_PARAM_DESC,
    _body_description,
    _param_description,
    _param_specs,
    generated_backend,
)

PRODUCTS = ("central", "mist", "clearpass")


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, method, path, *, params=None, json=None, kind=None, **extra):
        self.calls.append({"method": method, "path": path, "params": params})
        return {"ok": True}


def _tools(product: str, client=FakeClient):
    backend = generated_backend(product, client=client)
    return backend, {name: tool for name, tool in sdk_compat.tool_registry(backend).items()}


@pytest.mark.parametrize("product", PRODUCTS)
def test_every_parameter_with_words_to_give_has_a_description(product):
    _, tools = _tools(product)
    operations = load_manifest(product).operations
    for op in operations[:: max(1, len(operations) // 150)]:
        properties = tools[op["name"]].parameters["properties"]
        for spec in _param_specs(op):
            raw = next(p for p in op["parameters"] if p["name"] == spec.api and p["in"] == spec.location)
            has_words = bool(raw.get("description")) or spec.api.lower() in _FALLBACK_PARAM_DESCRIPTIONS
            if has_words or spec.api.lower().endswith("_id"):
                assert properties[spec.arg].get("description"), (op["name"], spec.arg)
            if properties[spec.arg].get("description"):
                assert len(properties[spec.arg]["description"]) <= _MAX_PARAM_DESC, (op["name"], spec.arg)


@pytest.mark.parametrize("product", PRODUCTS)
def test_body_lists_its_fields_with_required_ones_marked(product):
    _, tools = _tools(product)
    for op in load_manifest(product).operations:
        rb = op.get("request_body") or {}
        if not rb.get("properties"):
            continue
        text = tools[op["name"]].parameters["properties"]["body"]["description"]
        assert text.startswith("Fields: "), op["name"]
        for name in (rb.get("required_properties") or [])[:_MAX_BODY_FIELDS]:
            assert f"{name} (required)" in text, (op["name"], name)


def test_short_spec_description_appears_verbatim():
    _, tools = _tools("central")
    op = next(
        op
        for op in load_manifest("central").operations
        if any(p["name"] == "limit" and p.get("description") == "Limit for pagination." for p in op["parameters"])
    )
    assert tools[op["name"]].parameters["properties"]["limit"]["description"] == "Limit for pagination."


def test_long_or_multi_paragraph_description_is_trimmed():
    assert _param_description("next", "Cursor for the next page.\n\nMinimum value: 1") == "Cursor for the next page."
    first = "A first sentence long enough to stand on its own when the rest is cut."
    assert _param_description("x", f"{first} " + "word " * 80) == first
    assert len(_param_description("x", "word " * 80)) <= _MAX_PARAM_DESC
    assert _param_description("x", "line one\n  line two") == "line one line two"


def test_fallbacks_fill_bare_parameters():
    assert _param_description("org_id", "") == "The organization ID."
    assert _param_description("serial-number", "") == "The device serial number."
    assert _param_description("evpn_topology_id", "") == "The evpn topology ID."
    assert _param_description("org_id", "Filter results by organization identifier") == (
        "Filter results by organization identifier"
    )
    assert _param_description("classifier", "") == ""

    _, tools = _tools("mist")
    op = next(
        op
        for op in load_manifest("mist").operations
        if any(p["name"] == "org_id" and p["in"] == "path" and not p.get("description") for p in op["parameters"])
    )
    assert tools[op["name"]].parameters["properties"]["org_id"]["description"] == "The organization ID."


def test_body_description_is_bounded():
    many = {"properties": [f"f{i}" for i in range(50)], "required_properties": ["f40"]}
    text = _body_description(many)
    assert text.startswith("Fields: f40 (required), f0, f1")
    assert text.endswith(f", and {50 - _MAX_BODY_FIELDS} more.")
    assert _body_description({"description": "Request Body"}) == ""


async def test_optional_text_cursor_still_round_trips():
    client = FakeClient()
    backend, tools = _tools("mist", client=lambda: client)
    schema = tools["mist_search_site_alarms"].parameters["properties"]["search_after"]
    assert schema["description"].startswith("Pagination cursor")
    cursor = '[1700000000000, "a1"]'
    out = await sdk_compat.call_tool_raw(backend, "mist_search_site_alarms", {"site_id": "s1", "search_after": cursor})
    assert out == {"ok": True}
    assert client.calls[0]["params"]["search_after"] == cursor
