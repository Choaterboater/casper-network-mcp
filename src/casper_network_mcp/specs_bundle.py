"""The vendor OpenAPI documents bundled in this package, and their operations.

The files live in ``casper_network_mcp/specs`` with a ``MANIFEST.json``
written by ``scripts/refresh_specs.py``. Nothing here touches the network.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

__all__ = ["PRODUCTS", "Operation", "documents", "load_document", "manifest", "operations", "product_for", "specs_dir"]

PRODUCTS = ("central", "mist", "clearpass")
_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    operation_id: str
    tag: str
    query_params: tuple[str, ...]
    enums: dict[str, tuple[str, ...]]
    required_query: tuple[str, ...] = ()

    def __hash__(self) -> int:  # enums is a dict; identity is method + path
        return hash((self.method, self.path))


def specs_dir() -> Traversable:
    return files("casper_network_mcp") / "specs"


@functools.cache
def manifest() -> dict[str, Any]:
    return json.loads((specs_dir() / "MANIFEST.json").read_text(encoding="utf-8"))


def product_for(document: dict[str, Any]) -> str:
    """Which product a manifest entry describes.

    Mist comes from GitHub; ClearPass files are named ``clearpass-*.json``;
    every other registry document is Central.
    """
    if str(document.get("source_url", "")).startswith("https://raw.githubusercontent.com/mistsys/"):
        return "mist"
    if str(document.get("path", "")).startswith("clearpass-"):
        return "clearpass"
    return "central"


def documents(product: str | None = None) -> list[dict[str, Any]]:
    """Manifest entries, optionally for one product."""
    docs = manifest()["documents"]
    return [d for d in docs if product is None or product_for(d) == product]


@functools.cache
def load_document(path: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((specs_dir() / path).read_text(encoding="utf-8"))
    return data


def _resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """Follow a local ``$ref`` (``#/components/...``)."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < 10:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return {}
        target: Any = spec
        for part in ref[2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
        node = target
        depth += 1
    return node


def _enum_values(spec: dict[str, Any], schema: Any) -> tuple[str, ...]:
    schema = _resolve(spec, schema)
    if not isinstance(schema, dict):
        return ()
    values = schema.get("enum")
    if isinstance(values, list):
        return tuple(str(v) for v in values)
    items = _resolve(spec, schema.get("items"))
    if isinstance(items, dict) and isinstance(items.get("enum"), list):
        return tuple(str(v) for v in items["enum"])
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            found = _enum_values(spec, sub)
            if found:
                return found
    return ()


def _operations_in(spec: dict[str, Any]) -> list[Operation]:
    out: list[Operation] = []
    for path, item in (spec.get("paths") or {}).items():
        item = _resolve(spec, item)
        if not isinstance(item, dict):
            continue
        shared = [_resolve(spec, p) for p in item.get("parameters") or []]
        for method in _METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params: dict[tuple[str, str], dict[str, Any]] = {}
            for p in shared + [_resolve(spec, p) for p in op.get("parameters") or []]:
                if isinstance(p, dict) and p.get("name") and p.get("in"):
                    params[(p["in"], p["name"])] = p
            query = tuple(name for (where, name) in params if where == "query")
            required = tuple(name for (where, name), p in params.items() if where == "query" and p.get("required"))
            enums = {}
            for (where, name), p in params.items():
                if where in ("query", "path"):
                    values = _enum_values(spec, p.get("schema"))
                    if values:
                        enums[name] = values
            tags = op.get("tags") or []
            out.append(
                Operation(
                    method=method.upper(),
                    path=path,
                    operation_id=str(op.get("operationId") or ""),
                    tag=str(tags[0]) if tags else "",
                    query_params=query,
                    enums=enums,
                    required_query=required,
                )
            )
    return out


@functools.cache
def _operations(product: str) -> tuple[Operation, ...]:
    ops: list[Operation] = []
    for doc in documents(product):
        ops.extend(_operations_in(load_document(doc["path"])))
    return tuple(ops)


def operations(product: str) -> list[Operation]:
    """Every operation in the bundled documents for ``product`` (empty if none)."""
    if product not in PRODUCTS:
        return []
    return list(_operations(product))
