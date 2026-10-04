"""``lookup_api`` and ``list_api_families``: answers from the bundled vendor specs.

These are inner tools on a ``specs`` backend, reached through ``find_tool``.
They read only the files inside this package; they never call a product.
The ``lookup_api`` body is adapted from hpe-networking-mcp ``rag.py`` (MIT,
nowireless4u/hpe-networking-mcp).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from casper_network_mcp import specs_bundle, specs_index
from casper_network_mcp.core.kinds import tool_meta

__all__ = ["LOCAL_READ", "backend", "list_api_families", "lookup_api"]

#: Read-only, and local: these tools never reach a live system.
LOCAL_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

_PRODUCTS = ", ".join(specs_bundle.PRODUCTS)


def lookup_api(query: str, top_k: int = 10, product: str | None = None) -> list[dict[str, Any]]:
    """Look up the vendor API itself: endpoints, schemas, fields and allowed values.

    Exact answers from the official Central, Mist and ClearPass OpenAPI
    documents bundled with this server (no network call). Use it for "which
    endpoint does X and with what method", "what values does field Y accept",
    or "what fields does schema Z have". An empty list means the documents
    hold no confident answer.

    Args:
        query: A plain question, an exact ``METHOD /path``, or an exact
            operationId (for example "auth-type values for an auth profile",
            "GET /api/v1/sites/{site_id}/wlans" or "listSiteWlans").
        top_k: How many answers to return (1-20, default 10).
        product: Only this product: central, mist or clearpass.
    """
    if product is not None:
        product = product.strip().lower() or None
    if product is not None and product not in specs_bundle.PRODUCTS:
        return [{"error": f"Unknown product {product!r}. Use one of: {_PRODUCTS}."}]
    try:
        return specs_index.lookup(query, top_k=max(1, min(20, int(top_k))), product=product)
    except FileNotFoundError as exc:
        return [{"error": str(exc), "degraded": True}]


def list_api_families(product: str = "central", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """List the API families (documents and their sections) for one product.

    Shows what the bundled vendor API covers, with a few sample operations
    each, so you can choose what to look up next with ``lookup_api`` or
    ``find_tool``.

    Args:
        product: central, mist or clearpass (default central).
        limit: Families per page (1-200, default 50).
        offset: Families to skip (default 0).
    """
    name = (product or "central").strip().lower()
    if name not in specs_bundle.PRODUCTS:
        return {"error": f"Unknown product {product!r}. Use one of: {_PRODUCTS}."}
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    families: list[dict[str, Any]] = []
    for doc in specs_bundle.documents(name):
        spec = specs_bundle.load_document(doc["path"])
        ops = [
            (method.upper(), path, op)
            for path, item in (spec.get("paths") or {}).items()
            if isinstance(item, dict)
            for method, op in item.items()
            if method in ("get", "post", "put", "patch", "delete") and isinstance(op, dict)
        ]
        tags = Counter(str((op.get("tags") or [""])[0]) for _, _, op in ops)
        if len(tags) > 1 and len(specs_bundle.documents(name)) == 1:
            # One big document (Mist): its sections are the families.
            for tag, count in sorted(tags.items()):
                sample = [f"{m} {p}" for m, p, op in ops if str((op.get("tags") or [""])[0]) == tag][:3]
                families.append(
                    {"title": tag or "(untagged)", "document": doc["path"], "operations": count, "sample": sample}
                )
        else:
            families.append(
                {
                    "title": doc.get("title") or doc["path"],
                    "document": doc["path"],
                    "operations": len(ops),
                    "sample": [f"{m} {p}" for m, p, _ in ops[:3]],
                }
            )
    return {
        "product": name,
        "total": len(families),
        "offset": offset,
        "limit": limit,
        "families": families[offset : offset + limit],
    }


def backend() -> MCPServer:
    """The inner ``specs`` backend holding the two lookup tools."""
    server = MCPServer("specs")
    server.add_tool(lookup_api, annotations=LOCAL_READ, meta=tool_meta("read"))
    server.add_tool(list_api_families, annotations=LOCAL_READ, meta=tool_meta("read"))
    return server
