"""Every inner tool the router can reach, loaded once on first use.

The catalog holds the hand-written backends (Mist, Central x4, ClearPass), one
generated backend per product (one tool per bundled operation) and the
bundled-spec lookup backend. Backends are built the first time the router
needs them (``find_tool`` or an invoke), not when the server starts, and are
reused for the life of the process.

Every inner tool reaches its product through ``products._tools.client``,
which the server points at its own gated clients, so the catalog itself
holds no login and no gate.
"""

from __future__ import annotations

import functools
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer

from casper_network_mcp import sdk_compat
from casper_network_mcp.core.kinds import CHANGE_KINDS

__all__ = ["Catalog", "Entry", "catalog", "label_of", "tokens"]

_SPLIT = re.compile(r"[^a-z0-9]+")


def tokens(text: str) -> set[str]:
    """Lower-case word pieces of ``text`` (split on anything that is not a letter or digit)."""
    return {t for t in _SPLIT.split(text.lower()) if t}


def label_of(annotations: Any) -> str:
    """The plain label for a tool's annotation: read, diagnostic, write or destructive."""
    if annotations is None:
        return "write"
    if getattr(annotations, "read_only_hint", False):
        return "read"
    if getattr(annotations, "destructive_hint", False):
        return "destructive"
    if getattr(annotations, "title", None) == "Diagnostic":
        return "diagnostic"
    return "write"


@dataclass
class Entry:
    """One inner tool: where it lives, what it is, and the words it is found by."""

    name: str
    product: str
    server: MCPServer
    tool: Any
    kind: str
    label: str
    summary: str
    origin: str  # curated | generated | specs
    method: str = ""
    path: str = ""
    operation_id: str = ""
    name_tokens: set[str] = field(default_factory=set)
    summary_tokens: set[str] = field(default_factory=set)

    @property
    def schema(self) -> dict[str, Any]:
        params = getattr(self.tool, "parameters", None)
        return params if isinstance(params, dict) else {}


@dataclass
class Catalog:
    entries: dict[str, Entry]

    def get(self, name: str) -> Entry | None:
        return self.entries.get(name)


def _first_line(text: str | None) -> str:
    for line in (text or "").strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def _kind_of(tool: Any) -> str:
    meta = getattr(tool, "meta", None) or {}
    kind = meta.get("casper/change-kind")
    if kind not in CHANGE_KINDS:
        raise RuntimeError(f"tool {tool.name!r} has no change kind")
    return str(kind)


def _entry(name: str, product: str, server: MCPServer, tool: Any, origin: str, **extra: Any) -> Entry:
    summary = _first_line(getattr(tool, "description", None))
    return Entry(
        name=name,
        product=product,
        server=server,
        tool=tool,
        kind=_kind_of(tool),
        label=label_of(getattr(tool, "annotations", None)),
        summary=summary,
        origin=origin,
        name_tokens=tokens(name),
        summary_tokens=tokens(summary),
        **extra,
    )


def _getter(product: str) -> Any:
    from casper_network_mcp.products import _tools

    return lambda: _tools.client(product)


def _build() -> Catalog:
    from casper_network_mcp.openapi_gen.manifest import load_manifest
    from casper_network_mcp.openapi_gen.runtime import register_generated_tools
    from casper_network_mcp.products import hand_written_backends, specs_tools
    from casper_network_mcp.specs_bundle import PRODUCTS

    entries: dict[str, Entry] = {}

    def add(entry: Entry) -> None:
        if entry.name in entries:
            raise RuntimeError(f"two inner tools are named {entry.name!r}")
        entries[entry.name] = entry

    for product, server in hand_written_backends():
        for name, tool in sdk_compat.tool_registry(server).items():
            add(_entry(name, product, server, tool, "curated"))

    for product in PRODUCTS:
        manifest = load_manifest(product)
        server = MCPServer(f"{product}-generated")
        names = register_generated_tools(server, manifest, client=_getter(product))
        for name, op in zip(names, manifest.operations, strict=True):
            generated_tool = sdk_compat.get_tool(server, name)
            assert generated_tool is not None, name
            add(
                _entry(
                    name,
                    product,
                    server,
                    generated_tool,
                    "generated",
                    method=str(op["method"]).upper(),
                    path=manifest.base_path + str(op["path"]),
                    operation_id=str(op.get("operation_id") or ""),
                )
            )

    specs = specs_tools.backend()
    for name, tool in sdk_compat.tool_registry(specs).items():
        add(_entry(name, "specs", specs, tool, "specs"))
    return Catalog(entries)


_lock = threading.Lock()


@functools.cache
def _cached() -> Catalog:
    return _build()


def catalog() -> Catalog:
    """The catalog, built on first use and kept for the life of the process."""
    with _lock:
        return _cached()
