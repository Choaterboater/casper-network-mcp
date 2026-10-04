"""Every hand-written and generated tool name is unique across products (the router calls tools by name)."""

from __future__ import annotations

import collections

from casper_network_mcp import sdk_compat
from casper_network_mcp.openapi_gen.runtime import generated_backend
from casper_network_mcp.products import hand_written_backends


def test_tool_names_are_unique_across_every_backend():
    names: collections.Counter[str] = collections.Counter()
    for _, backend in hand_written_backends():
        names.update(sdk_compat.tool_names(backend))
    for product in ("central", "mist", "clearpass"):
        names.update(sdk_compat.tool_names(generated_backend(product, client=lambda: None)))
    assert not [n for n, count in names.items() if count > 1]


def test_hand_written_backends_cover_three_products():
    products = [product for product, _ in hand_written_backends()]
    assert set(products) == {"central", "mist", "clearpass"}
    assert products.count("central") == 4
