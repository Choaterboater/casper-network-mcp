"""Send one generated call through the product client.

Rewritten from hpe-networking-mcp ``openapi_gen/http_exec.py``. The source built its own pooled HTTP client
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


def _project_fields(data: Any, fields: str) -> Any:
    """Keep only the named top-level keys, so ``fields`` actually shrinks the reply.

    Some Mist endpoints declare ``fields`` but still answer with the whole
    record. Passing the parameter through is harmless, so the reply is
    projected here as well. A dotted name (``radio_stat.channel``) selects its
    top-level key. Records in the reply's main list are projected too.
    """
    names = [token.strip().split(".", 1)[0] for token in fields.split(",") if token.strip()]
    if not names:
        return data

    def project(record: Any) -> Any:
        if not isinstance(record, dict):
            return record
        return {key: record[key] for key in names if key in record}

    if isinstance(data, list):
        return [project(record) for record in data]
    if isinstance(data, dict):
        primary = max(
            (
                key
                for key, value in data.items()
                if isinstance(value, list) and value and all(isinstance(item, dict) for item in value)
            ),
            key=lambda key: len(data[key]),
            default=None,
        )
        if primary is not None:
            return {**data, primary: [project(record) for record in data[primary]]}
        return project(data)
    return data


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
    fields = query.get("fields") if isinstance(query, dict) else None
    if isinstance(fields, str) and fields.strip():
        data = _project_fields(data, fields)
    return _shaped(data)
