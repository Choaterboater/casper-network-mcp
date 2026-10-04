"""``access_check``: what each product login can do (casper/access-check v2).

Filled in by Task 12 (scopes); until then every product with a login reports
``access: "unknown"``.
"""

from __future__ import annotations

from typing import Any

from casper_network_mcp.core.gate import Gate

__all__ = ["CONTRACT", "access_check"]

CONTRACT = "casper/access-check v2"
PRODUCTS = ("central", "mist", "clearpass")


def _server_gate(gate: Gate) -> dict[str, str]:
    return {"flag": "--read-only", "state": "off" if gate.read_only else "on"}


async def access_check(clients: dict[str, Any], gate: Gate) -> dict[str, Any]:
    products: list[dict[str, Any]] = []
    for product in PRODUCTS:
        entry: dict[str, Any] = {"product": product, "access": "unknown"}
        if not clients[product].has_login:
            entry["login"] = "missing"
        entry["server_gate"] = _server_gate(gate)
        products.append(entry)
    return {"contract": CONTRACT, "products": products}
