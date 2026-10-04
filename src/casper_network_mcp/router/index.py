"""Every inner tool the router can reach, and the prebuilt word index find_tool reads.

Two things live here:

* the **catalog**: the real tools (with their backends), needed to run one;
* the **word index** (``router/index.json``, shipped in the wheel): for each
  tool only what ``find_tool`` needs (name, product, first line, kind, label,
  words). ``find_tool`` answers from it without building any backend, so the
  first question is fast. ``scripts/build_index.py`` writes it and a test
  checks it matches the tools; if the file is missing or unreadable it is
  rebuilt in memory from the catalog.

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
import json
import re
import threading
from dataclasses import dataclass, field
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from casper_network_mcp.core.kinds import CHANGE_KINDS

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

__all__ = [
    "INDEX_VERSION",
    "Catalog",
    "Entry",
    "IndexEntry",
    "build_index",
    "catalog",
    "dumps_index",
    "fold",
    "index_of",
    "label_of",
    "load_index",
    "tokens",
]

#: Bumped whenever the index shape or the word rules (``tokens``/``fold``) change.
INDEX_VERSION = 1

_SPLIT = re.compile(r"[^a-z0-9]+")


def fold(word: str) -> str:
    """One word form for singular and plural (``sites`` -> ``site``, ``policies`` -> ``policy``)."""
    if len(word) <= 3 or not word.endswith("s") or word.endswith("ss"):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    return word[:-1]


def tokens(text: str) -> set[str]:
    """Lower-case word pieces of ``text``, plurals folded (split on anything not a letter or digit)."""
    return {fold(t) for t in _SPLIT.split(text.lower()) if t}


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


def _scope_params(tool: Any) -> list[str]:
    params = getattr(tool, "parameters", None)
    props = params.get("properties") if isinstance(params, dict) else None
    return sorted({str(p).lower().split("_")[0] for p in props or {}})


def _getter(product: str) -> Any:
    from casper_network_mcp.products import _tools

    return lambda: _tools.client(product)


def _build() -> Catalog:
    from mcp.server.mcpserver import MCPServer

    from casper_network_mcp import sdk_compat
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


# ── the word index ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IndexEntry:
    """What find_tool knows about one tool, without its backend."""

    name: str
    product: str
    summary: str
    kind: str
    label: str
    origin: str
    method: str
    path: str
    operation_id: str
    name_tokens: frozenset[str]
    summary_tokens: frozenset[str]
    params: frozenset[str]


@dataclass(frozen=True)
class WordIndex:
    entries: tuple[IndexEntry, ...]
    by_name: dict[str, IndexEntry]
    #: word -> positions in ``entries`` of the tools with that word in their name
    name_postings: dict[str, tuple[int, ...]]


def build_index(source: Catalog | None = None) -> dict[str, Any]:
    """The word index as plain data, from the catalog (what ``router/index.json`` holds)."""
    tools: list[dict[str, Any]] = []
    for entry in sorted((source or catalog()).entries.values(), key=lambda e: e.name):
        tools.append(
            {
                "name": entry.name,
                "product": entry.product,
                "summary": entry.summary,
                "kind": entry.kind,
                "label": entry.label,
                "origin": entry.origin,
                "method": entry.method,
                "path": entry.path,
                "operation_id": entry.operation_id,
                "name_words": sorted(entry.name_tokens),
                "summary_words": sorted(entry.summary_tokens - entry.name_tokens),
                "params": _scope_params(entry.tool),
            }
        )
    postings: dict[str, list[int]] = {}
    for i, tool in enumerate(tools):
        for word in tool["name_words"]:
            postings.setdefault(word, []).append(i)
    return {"version": INDEX_VERSION, "tools": tools, "name_postings": dict(sorted(postings.items()))}


def dumps_index(data: dict[str, Any]) -> str:
    """The exact text written to ``router/index.json`` (stable, so a check can compare it)."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=True) + "\n"


def index_of(source: Catalog) -> WordIndex:
    """A word index over ``source`` (tests use it with a small catalog)."""
    return _from_data(build_index(source))


def _from_data(data: dict[str, Any]) -> WordIndex:
    entries = tuple(
        IndexEntry(
            name=t["name"],
            product=t["product"],
            summary=t["summary"],
            kind=t["kind"],
            label=t["label"],
            origin=t["origin"],
            method=t["method"],
            path=t["path"],
            operation_id=t["operation_id"],
            name_tokens=frozenset(t["name_words"]),
            summary_tokens=frozenset(t["name_words"]) | frozenset(t["summary_words"]),
            params=frozenset(t["params"]),
        )
        for t in data["tools"]
    )
    postings = {word: tuple(ids) for word, ids in data["name_postings"].items()}
    return WordIndex(entries, {e.name: e for e in entries}, postings)


def _read_shipped() -> dict[str, Any] | None:
    try:
        data = json.loads((files("casper_network_mcp.router") / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return None
    return data


_index_lock = threading.Lock()


@functools.cache
def _cached_index() -> WordIndex:
    data = _read_shipped()
    try:
        return _from_data(data if data is not None else build_index())
    except (KeyError, TypeError):
        return _from_data(build_index())


def load_index() -> WordIndex:
    """The shipped word index, or one rebuilt in memory if it is missing, unreadable or out of date."""
    with _index_lock:
        return _cached_index()
