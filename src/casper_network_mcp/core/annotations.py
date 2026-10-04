"""MCP tool annotations: the four labels every tool carries.

Copied from hpe-networking-mcp (MIT, nowireless4u/hpe-networking-mcp); only the
four label constants are kept.
"""

from mcp.types import ToolAnnotations

__all__ = ["DESTRUCTIVE", "DIAGNOSTIC", "READ_ONLY", "WRITE"]

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

DIAGNOSTIC = ToolAnnotations(
    title="Diagnostic",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
