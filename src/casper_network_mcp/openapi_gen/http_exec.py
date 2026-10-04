"""Send one generated call through the product client.

Rewritten from hpe-networking-mcp ``openapi_gen/http_exec.py`` (MIT,
nowireless4u/hpe-networking-mcp). The source built its own pooled HTTP client
and its own write gate; here there is neither. Every generated call goes
through the product client's ``request()``, which runs the gate first, then
the path check, then sends, so a generated tool can never reach the network
by a road the hand-written tools don't use.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import httpx

from casper_network_mcp.core.budget import bound_collection_response, clamp_limit
from casper_network_mcp.core.errors import ToolError
from casper_network_mcp.core.redact import redact_tool_error_text

__all__ = ["ProductClient", "send"]


class ProductClient(Protocol):
    """What a product client offers generated tools (Task 6 clients, or a test fake)."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        kind: str | None = None,
        headers: dict[str, str] | None = None,
        content_type: str = "application/json",
        path_args: dict[str, Any] | None = None,
    ) -> Any: ...


def _shaped(data: Any) -> dict[str, Any]:
    if data is None:
        return {"ok": True}
    bounded = bound_collection_response(data, limit=clamp_limit(None), offset=0)
    return bounded if isinstance(bounded, dict) else {"result": bounded}


async def send(
    client: Callable[[], ProductClient],
    *,
    product: str,
    method: str,
    path: str,
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any,
    content_type: str,
    kind: str,
    path_args: dict[str, Any],
) -> dict[str, Any]:
    """Call ``client().request(...)`` and turn every failure into ``{"error": ...}``."""
    try:
        data = await client().request(
            method,
            path,
            params=query or None,
            json=body,
            kind=kind,
            headers=headers or None,
            content_type=content_type,
            path_args=path_args,
        )
    except ToolError as exc:
        return exc.as_error()
    except httpx.HTTPError as exc:
        return {"error": redact_tool_error_text(f"Could not reach {product}: {exc}")}
    except ValueError as exc:  # an unsafe path or a body that does not fit its type
        return {"error": str(exc)}
    return _shaped(data)
