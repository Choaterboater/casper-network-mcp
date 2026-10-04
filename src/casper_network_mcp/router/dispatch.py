"""``invoke_read_tool`` and ``invoke_tool``: run one inner tool by name.

Ported from hpe-networking-mcp ``mcp_servers/tool_router.py`` (``_dispatch_tool``,
``_dispatch_read_tool``, ``invoke_read_tool``, ``invoke_tool``; MIT,
nowireless4u/hpe-networking-mcp). Cut: the per-platform write switches, the
global read-only env switch, execution contracts and the rate gate. What
stays: the call goes through the owning backend's tool manager (so arguments
are checked and coerced), a raised error comes back as ``{"error": ...}`` with
secrets hidden, and every result is bounded (with a signed cursor for reads).

Nothing here decides whether a change may be sent: the product client's gate
does that (the read-only pin, and the login's scopes), so ``invoke_tool``
and a direct call take the same road.
"""

from __future__ import annotations

from typing import Any

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.budget import CursorError, bound_router_response, decode_cursor
from casper_network_mcp.core.kinds import READ_POSTS, TROUBLESHOOT_OPS, listed_template
from casper_network_mcp.core.redact import redact_tool_error_text
from casper_network_mcp.router.index import Entry, catalog

__all__ = ["invoke_read_tool", "invoke_tool", "is_read_tool"]


def _strip_nulls(arguments: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (arguments or {}).items() if v is not None}


def _unknown(name: str) -> dict[str, Any]:
    return {
        "error": f"Unknown tool '{name}'. Use find_tool to find the right name.",
        "tool": name,
        "status": "unknown_tool",
    }


def is_read_tool(entry: Entry) -> bool:
    """True when ``entry`` only reads (or runs a check that changes nothing).

    A tool of kind ``read``, or ``troubleshoot`` without a destructive label.
    A generated tool must also be a GET, a reviewed read-only POST
    (``READ_POSTS``) or a hand-checked troubleshooting operation.
    """
    if not (entry.kind == "read" or (entry.kind == "troubleshoot" and entry.label != "destructive")):
        return False
    if entry.origin != "generated":
        return True
    if entry.method == "GET":
        return True
    template = listed_template(entry.method, entry.path)
    return template is not None and (
        (entry.method, template) in READ_POSTS or (entry.method, template) in TROUBLESHOOT_OPS
    )


async def _run(entry: Entry, args: dict[str, Any], *, offset: int = 0, cursor_ok: bool = False) -> Any:
    try:
        result = await sdk_compat.call_tool_raw(entry.server, entry.name, args)
    except Exception as exc:  # noqa: BLE001 - every failure comes back as a plain error
        # The SDK hides an unexpected error's text behind "Error executing
        # tool"; the cause says what went wrong. Secrets are hidden either way.
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        result = {"error": f"{type(cause).__name__}: {redact_tool_error_text(str(cause))}"}
    return bound_router_response(
        result,
        offset=offset,
        enable_cursor=cursor_ok,
        tool_name=entry.name,
        tool_arguments=args,
    )


async def invoke_read_tool(name: str, arguments: dict[str, Any] | None = None, cursor: str | None = None) -> Any:
    """Run a read tool; anything else is refused with ``not_a_read_tool``."""
    entry = catalog().get(name)
    if entry is None:
        return _unknown(name)
    if not is_read_tool(entry):
        return {
            "error": "not_a_read_tool",
            "tool": name,
            "kind": entry.kind,
            "detail": f"{name} can change things ({entry.kind}); call it with invoke_tool.",
        }
    args = _strip_nulls(arguments)
    offset = 0
    if cursor is not None:
        try:
            offset = decode_cursor(cursor, name=name, arguments=args)
        except CursorError as exc:
            return {"error": str(exc), "tool": name, "status": "invalid_cursor"}
    return await _run(entry, args, offset=offset, cursor_ok=True)


async def invoke_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Run any inner tool. The product client's gate decides whether a change is sent."""
    entry = catalog().get(name)
    if entry is None:
        return _unknown(name)
    return await _run(entry, _strip_nulls(arguments))
